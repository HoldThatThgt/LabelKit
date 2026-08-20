"""Offline unit tests for the labelkit/cli package.

Pure-logic coverage: exit-code mapping, argparse surface, rubric printing,
referenced-profile collection. Tests that exercise the `validate` / `run`
subcommands end-to-end need the M1 loader (and, for `run`, the whole module
graph); they auto-skip via importorskip until those modules land — no mock
LLMs, and no LLM is ever reached (both scenarios fail before any call).
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import logging
import runpy
import sys
import tomllib
from importlib import resources
from pathlib import Path

import pytest

import labelkit.cli as cli
from labelkit.cli import commands as cli_commands
from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    CliOverrides,
    ConsoleConfig,
    Criterion,
    DedupConfig,
    EmbeddingProfile,
    ExtractConfig,
    FrameAnnotateConfig,
    FrameClassifyConfig,
    GenerateConfig,
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
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import (
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
from labelkit.common.runtime.llm_client import LLMClient, ProbeResult
from labelkit.orchestration.factory import build_stages
from labelkit.orchestration.profile_usage import referenced_profiles
from labelkit.orchestration.runtime import execute_run, probe_referenced_profiles

# labelkit.cli 把 main 这个**函数**导出到包命名空间，遮住了同名子模块——兜底出口
# 的 monkeypatch 要打在真模块对象上。
cli_main = importlib.import_module("labelkit.cli.main")


EXPECTED_PRODUCTION_PY = {
    "labelkit/__init__.py",
    "labelkit/cli/__init__.py",
    "labelkit/cli/commands.py",
    "labelkit/cli/console.py",
    "labelkit/cli/main.py",
    "labelkit/cli/parser.py",
    "labelkit/common/config/__init__.py",
    "labelkit/common/config/_classviews.py",   # M1 拆分（2026-08-14 规则整改：每文件 ≤2000 行）
    "labelkit/common/config/_collect.py",
    "labelkit/common/config/_constraints.py",
    "labelkit/common/config/_generate_stream_constraints.py",  # v1.16 时间流约束簇
    "labelkit/common/config/_rubrics.py",
    "labelkit/common/config/_schemas.py",
    "labelkit/common/config/_sections.py",
    "labelkit/common/config/loader.py",
    "labelkit/common/config/model.py",
    "labelkit/common/contracts/stage.py",
    "labelkit/common/contracts/types.py",
    "labelkit/common/errors.py",
    "labelkit/common/extensions/hooks.py",
    "labelkit/common/observability/console_format.py",
    "labelkit/common/observability/obslog.py",
    "labelkit/common/runtime/budget.py",           # v1.11 (CONTRACTS §7.17)
    "labelkit/common/runtime/declare.py",          # v1.16 DECLARE 直接语义
    "labelkit/common/runtime/llm_client.py",
    "labelkit/common/runtime/schema_engine.py",
    "labelkit/common/runtime/sequence_planner.py", # v1.16 联合 CP-SAT
    "labelkit/common/runtime/temporal.py",         # v1.16 微秒与日历窗
    "labelkit/operators/annotate.py",
    "labelkit/operators/classify.py",
    "labelkit/operators/dedup.py",
    "labelkit/operators/emitter.py",
    "labelkit/operators/extract.py",
    "labelkit/operators/generate.py",
    "labelkit/operators/generate_stream.py",       # M6 时间流纯逻辑物理拆分
    "labelkit/operators/ingest.py",
    "labelkit/operators/quality.py",
    "labelkit/operators/segment.py",
    "labelkit/operators/stitch.py",
    "labelkit/operators/verify.py",
    "labelkit/orchestration/__init__.py",
    "labelkit/orchestration/factory.py",
    "labelkit/orchestration/orchestrator.py",
    "labelkit/orchestration/profile_usage.py",
    "labelkit/orchestration/runtime.py",
}

EXPECTED_TEST_PY = {
    "tests/cli/test_cli.py",
    "tests/cli/test_console.py",
    "tests/common/config/test_config.py",
    "tests/common/config/test_loader_generate_stream.py",   # v1.13 (SPEC-stream-generation §3.1)
    "tests/common/contracts/test_stage.py",
    "tests/common/contracts/test_types.py",
    "tests/common/extensions/test_hooks.py",
    "tests/common/observability/test_console_format.py",
    "tests/common/observability/test_obslog.py",
    "tests/common/runtime/test_budget.py",         # v1.11 (CONTRACTS §7.17)
    "tests/common/runtime/test_declare.py",        # v1.16 十五模板与 occurrence
    "tests/common/runtime/test_llm_client.py",
    "tests/common/runtime/test_schema_engine.py",
    "tests/common/runtime/test_sequence_planner.py",  # v1.16 联合模型 oracle
    "tests/common/runtime/test_temporal.py",       # v1.16 微秒、日历与重发
    "tests/common/test_errors.py",
    "tests/conftest.py",
    "tests/hook_samples.py",
    "tests/integration/test_annotate_llm.py",
    "tests/integration/test_budget_llm.py",
    "tests/integration/test_classify_llm.py",
    "tests/integration/test_frame_llm.py",         # v1.12 (SPEC-frame-annotation §3.9)
    "tests/integration/test_generate_llm.py",
    "tests/integration/test_generate_stream_llm.py",  # v1.13 (SPEC-stream-generation §3.9)
    "tests/integration/test_key_pool_llm.py",
    "tests/integration/test_llm_client_llm.py",
    "tests/integration/test_quality_llm.py",
    "tests/integration/test_schema_engine_llm.py",
    "tests/integration/test_stitch_llm.py",
    "tests/integration/test_stream_llm.py",
    "tests/integration/test_verify_llm.py",
    "tests/operators/test_annotate.py",
    "tests/operators/test_classify.py",
    "tests/operators/test_dedup.py",
    "tests/operators/test_emitter.py",
    "tests/operators/test_extract.py",
    "tests/operators/test_generate.py",
    "tests/operators/test_generate_stream.py",   # v1.13 时间流形态（wave 三）
    "tests/operators/test_ingest.py",
    "tests/operators/test_quality.py",
    "tests/operators/test_segment.py",
    "tests/operators/test_stitch.py",
    "tests/operators/test_verify.py",
    "tests/orchestration/test_orchestrator.py",
}

REMOVED_MODULES = (
    "labelkit.annotate",
    "labelkit.classify",
    "labelkit.config",
    "labelkit.dedup",
    "labelkit.emitter",
    "labelkit.errors",
    "labelkit.extract",
    "labelkit.generate",
    "labelkit.hooks",
    "labelkit.ingest",
    "labelkit.llm_client",
    "labelkit.obslog",
    "labelkit.orchestrator",
    "labelkit.quality",
    "labelkit.schema_engine",
    "labelkit.segment",
    "labelkit.stage",
    "labelkit.types",
    "labelkit.verify",
)


def test_package_layout_matches_frozen_spec():
    root = Path(__file__).resolve().parents[2]
    production = {
        path.relative_to(root).as_posix()
        for path in (root / "labelkit").rglob("*.py")
        if "__pycache__" not in path.parts
    }
    tests = {
        path.relative_to(root).as_posix()
        for path in (root / "tests").rglob("*.py")
        if "__pycache__" not in path.parts
    }

    assert production == EXPECTED_PRODUCTION_PY
    assert tests == EXPECTED_TEST_PY
    assert {path.name for path in (root / "labelkit").glob("*.py")} == {"__init__.py"}
    assert not (root / "labelkit" / "config").exists()

    for module in REMOVED_MODULES:
        try:
            spec = importlib.util.find_spec(module)
        except ModuleNotFoundError:
            spec = None
        assert spec is None, f"legacy import path still resolves: {module}"


def test_package_layout_dependency_direction():
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []

    for relative in sorted(EXPECTED_PRODUCTION_PY):
        if relative == "labelkit/__init__.py":
            continue
        path = root / relative
        imports: list[str] = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=relative)):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        if relative.startswith("labelkit/common/"):
            forbidden = ("labelkit.cli", "labelkit.operators", "labelkit.orchestration")
        elif relative.startswith("labelkit/operators/"):
            forbidden = ("labelkit.cli", "labelkit.orchestration")
        elif relative.startswith("labelkit/orchestration/"):
            forbidden = ("labelkit.cli",)
        elif relative.startswith("labelkit/cli/"):
            forbidden = ("labelkit.operators",)
        else:
            forbidden = ()

        for imported in imports:
            if imported.startswith(forbidden):
                violations.append(f"{relative}: forbidden import {imported}")

        if relative.startswith("labelkit/operators/"):
            own_module = relative.removesuffix(".py").replace("/", ".")
            # verify 的修复面懒加载白名单（CONTRACTS §1.1）：v1.12 第四向
            # classify.classify_frames（帧产物回收补跑）加入，3→4。
            if own_module == "labelkit.operators.verify":
                allowed_operator_calls = {
                    "labelkit.operators.annotate",
                    "labelkit.operators.classify",
                    "labelkit.operators.extract",
                    "labelkit.operators.segment",
                }
            elif own_module == "labelkit.operators.generate":
                # M6 的物理拆分：generate_stream 是同一算子的纯逻辑承载文件。
                allowed_operator_calls = {"labelkit.operators.generate_stream"}
            else:
                allowed_operator_calls = set()
            for imported in imports:
                if (imported.startswith("labelkit.operators.")
                        and imported != own_module
                        and imported not in allowed_operator_calls):
                    violations.append(f"{relative}: operator dependency {imported}")

    assert violations == []

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
    assert "ConfigError: 2 config error(s) (all aggregated)" in err
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


# ── --console (v1.10, spec §7.7 / 5.1; U5/U27) ─────────────────────────────


@pytest.mark.parametrize("value", ["auto", "rich", "plain"])
def test_parser_run_console_lands_in_overrides(value):
    args = cli.build_parser().parse_args(
        ["run", "--config", "c.toml", "--project", "p.toml", "--console", value])
    assert cli._overrides_from_args(args).console == value


def test_parser_run_console_default_none():
    args = cli.build_parser().parse_args(
        ["run", "--config", "c.toml", "--project", "p.toml"])
    assert cli._overrides_from_args(args).console is None


def test_parser_validate_accepts_console():
    args = cli.build_parser().parse_args(
        ["validate", "--config", "c.toml", "--project", "p.toml",
         "--console", "rich"])
    assert args.console == "rich"

    args = cli.build_parser().parse_args(
        ["validate", "--config", "c.toml", "--project", "p.toml"])
    assert args.console is None


def test_parser_console_invalid_value_rejected():
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(
            ["run", "--config", "c.toml", "--project", "p.toml",
             "--console", "fancy"])
    assert excinfo.value.code == EXIT_CONFIG
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["validate", "--config", "c.toml", "--project", "p.toml",
             "--console", "fancy"])


# ── --limit validation (spec 3.1.2 CLI dict / 3.1.5 no runtime config errors) ─


@pytest.mark.parametrize("bad", ["0", "-1", "-100", "abc", "1.5"])
def test_limit_rejects_non_positive_or_non_int(bad, capsys):
    """Zero/negative/non-integer --limit is a usage error → exit 2 (spec §2.4),
    never a runtime ValueError inside the orchestrator (spec 3.1.5)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.build_parser().parse_args(
            ["run", "--config", "c.toml", "--project", "p.toml", "--limit", bad])
    assert excinfo.value.code == EXIT_CONFIG
    assert "expected an integer >= 1" in capsys.readouterr().err


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
    [("default:text", "default_text.toml"), ("default:ui", "default_ui.toml"),
     ("default:trajectory", "default_trajectory.toml")],   # v1.8 (S29, §7.12)
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
    assert set(out) == {"default:text", "default:ui", "default:trajectory"}


# ── referenced_profiles (validate --probe helper) ──────────────────────────


def _profile(name: str) -> LLMProfile:
    return LLMProfile(name=name, provider="anthropic", base_url="https://x",
                      model="m", api_key_env="K")


def _cfg(**kw) -> ResolvedConfig:
    base = dict(
        tool=ToolConfig(),
        console=ConsoleConfig(),
        llm_profiles={n: _profile(n) for n in ("default", "judge", "fixer")},
        embedding_profiles={"emb": EmbeddingProfile(
            name="emb", base_url="https://x", model="e", api_key_env="K")},
        run=RunConfig(output="out.jsonl", modality="text", input="in.jsonl"),
        input=InputConfig(),
        stream=StreamConfig(),
        dedup=DedupConfig(),
        segment=SegmentConfig(),
        stitch=StitchConfig(),
        extract=ExtractConfig(),
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
    llms, embs = referenced_profiles(_cfg())
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
    llms, embs = referenced_profiles(cfg)
    assert llms == ["default", "judge", "fixer"]  # order-preserving, deduplicated
    assert embs == ["emb"]


def test_referenced_profiles_disabled_stages_not_probed():
    cfg = _cfg(
        quality=QualityConfig(enabled=False),
        annotate=AnnotateConfig(enabled=True, llm="judge", instruction="标注"),
        verify=VerifyConfig(enabled=False, llm="fixer"),  # disabled → not referenced
    )
    llms, embs = referenced_profiles(cfg)
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
    llms, _ = referenced_profiles(cfg)
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
    llms, _ = referenced_profiles(cfg)
    assert llms == ["default"]  # judges are NOT referenced in pointwise mode


def test_referenced_profiles_verify_judges_replace_verify_llm():
    cfg = _cfg(
        quality=QualityConfig(enabled=False),
        annotate=AnnotateConfig(enabled=True, llm="default", instruction="标注"),
        verify=VerifyConfig(enabled=True, llm="fixer", judges=("judge",)),
    )
    llms, _ = referenced_profiles(cfg)
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
    llms, _ = referenced_profiles(cfg)
    # classify precedes quality/annotate (chain order); "default" deduplicated
    assert llms == ["judge", "default"]


def test_referenced_profiles_classify_disabled_not_referenced():
    # Loader rule-12 semantics: the guard is `classify.enabled`, so a disabled
    # stage's profile reference ("fixer") is never keyed nor probed.
    cfg = _cfg(classify=ClassifyConfig(enabled=False, llm="fixer"))
    llms, _ = referenced_profiles(cfg)
    assert llms == ["default"]


def test_build_stages_inserts_classify_after_dedup_before_quality():
    from labelkit.operators.classify import ClassifyStage

    cfg = _cfg(classify=_classify_cfg())
    stages = build_stages(cfg)
    assert [s.name for s in stages] == ["dedup", "classify", "quality", "annotate"]
    assert isinstance(stages[1], ClassifyStage)


def test_build_stages_without_classify_unchanged():
    stages = build_stages(_cfg())
    assert [s.name for s in stages] == ["dedup", "quality", "annotate"]


def test_build_stages_frame_only_or_gate_includes_classify_stage():
    """v1.12 或门（SPEC §3.2）：classify.enabled=false ∧ frame.classify.enabled=true
    时 ClassifyStage 仍进链承载帧 pass（槽位不变：dedup 后、quality 前）；
    序列级判决由 stage 内 classify.enabled 门静默跳过（test_classify 守护）。"""
    from labelkit.operators.classify import ClassifyStage

    cfg = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
               frame_classify=FrameClassifyConfig(enabled=True, llm="default"))
    stages = build_stages(cfg)
    assert [s.name for s in stages] == ["segment", "dedup", "classify",
                                        "quality", "annotate"]
    assert isinstance(stages[2], ClassifyStage)


# ── v1.8 stream (S30 reference sets + chain slots §7.9/§7.12) ───────────────


def test_referenced_profiles_segment_strategy_gate():
    """S30: segment.llm joins the probe set ONLY when segment is enabled AND
    strategy ∈ {llm, hybrid} — the rules strategy makes zero segment LLM calls,
    so its profile is never probed; chain position: segment heads the list."""
    hybrid = _cfg(segment=SegmentConfig(enabled=True, strategy="hybrid", llm="judge"))
    assert referenced_profiles(hybrid)[0] == ["judge", "default"]

    llm_only = _cfg(segment=SegmentConfig(enabled=True, strategy="llm", llm="judge"))
    assert referenced_profiles(llm_only)[0] == ["judge", "default"]

    rules = _cfg(segment=SegmentConfig(enabled=True, strategy="rules", llm="judge"))
    assert referenced_profiles(rules)[0] == ["default"]

    disabled = _cfg(segment=SegmentConfig(enabled=False, strategy="hybrid",
                                          llm="judge"))
    assert referenced_profiles(disabled)[0] == ["default"]


def test_referenced_profiles_extract_always_when_enabled():
    """S30: extract.llm is referenced whenever extract is enabled; slot follows
    the chain order — after classify, before quality."""
    cfg = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
               extract=ExtractConfig(enabled=True, llm="fixer"),
               classify=_classify_cfg(llm="judge"))
    llms, _ = referenced_profiles(cfg)
    assert llms == ["judge", "fixer", "default"]


def test_build_stages_stream_inserts_segment_and_extract():
    """v1.8 (§7.12): segment heads the chain (before dedup); extract slots after
    classify / before quality (§7.9). Gated on the sibling modules landing."""
    pytest.importorskip("labelkit.operators.segment")
    pytest.importorskip("labelkit.operators.extract")
    from labelkit.operators.extract import ExtractStage
    from labelkit.operators.segment import SegmentStage

    cfg = _cfg(segment=SegmentConfig(enabled=True),
               extract=ExtractConfig(enabled=True),
               classify=_classify_cfg())
    stages = build_stages(cfg)
    assert [s.name for s in stages] == ["segment", "dedup", "classify", "extract",
                                        "quality", "annotate"]
    assert isinstance(stages[0], SegmentStage)
    assert isinstance(stages[3], ExtractStage)


# ── v1.9 stitch (T16/T17 reference set + §7.12 chain slot) ───────────────────


def test_referenced_profiles_stitch_enabled_only():
    """v1.9: stitch.llm joins the probe set iff stitch is enabled (pure-text
    judgment — referenced regardless of strategy/vision); slot follows the
    chain order — after segment, before classify."""
    on = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
              stitch=StitchConfig(enabled=True, llm="judge"))
    assert referenced_profiles(on)[0] == ["judge", "default"]

    chain = _cfg(segment=SegmentConfig(enabled=True, strategy="hybrid",
                                       llm="fixer"),
                 stitch=StitchConfig(enabled=True, llm="judge"),
                 classify=_classify_cfg(llm="default"))
    assert referenced_profiles(chain)[0] == ["fixer", "judge", "default"]

    off = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
               stitch=StitchConfig(enabled=False, llm="judge"))
    assert referenced_profiles(off)[0] == ["default"]


# ── v1.12 frame granularity (帧级两 llm 键入探测集；vision 集分列见 test_config) ──


def test_referenced_profiles_frame_classify_enabled_only():
    """v1.12: frame.classify.llm joins the probe set iff enabled; slot follows
    the chain order — right after sequence-level classify."""
    on = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
              classify=_classify_cfg(llm="default"),
              frame_classify=FrameClassifyConfig(enabled=True, llm="judge"))
    assert referenced_profiles(on)[0] == ["default", "judge"]

    off = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
               frame_classify=FrameClassifyConfig(enabled=False, llm="judge"))
    assert referenced_profiles(off)[0] == ["default"]


def test_referenced_profiles_frame_annotate_enabled_only():
    """v1.12: frame.annotate.llm joins the probe set iff enabled; slot follows
    the chain order — right after sequence-level annotate."""
    on = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
              frame_annotate=FrameAnnotateConfig(enabled=True, llm="judge",
                                                 instruction="标注帧意图"))
    assert referenced_profiles(on)[0] == ["default", "judge"]

    off = _cfg(segment=SegmentConfig(enabled=True, strategy="rules"),
               frame_annotate=FrameAnnotateConfig(enabled=False, llm="judge"))
    assert referenced_profiles(off)[0] == ["default"]


def test_build_stages_inserts_stitch_between_segment_and_dedup():
    """v1.9 (§7.12): StitchStage instantiates between segment and dedup when
    enabled; disabled keeps the v1.8 list byte-identical."""
    from labelkit.operators.stitch import StitchStage

    cfg = _cfg(segment=SegmentConfig(enabled=True),
               stitch=StitchConfig(enabled=True),
               extract=ExtractConfig(enabled=True))
    stages = build_stages(cfg)
    assert [s.name for s in stages] == ["segment", "stitch", "dedup", "extract",
                                        "quality", "annotate"]
    assert isinstance(stages[1], StitchStage)

    off = _cfg(segment=SegmentConfig(enabled=True))
    assert [s.name for s in build_stages(off)] == ["segment", "dedup", "quality",
                                                   "annotate"]


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
    pytest.importorskip("labelkit.common.config.loader")
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
    pytest.importorskip("labelkit.common.config.loader")
    rc = cli.main([
        "validate",
        "--config", str(tmp_path / "nope-config.toml"),
        "--project", str(tmp_path / "nope-project.toml"),
    ])
    assert rc == EXIT_CONFIG
    assert capsys.readouterr().err  # some error text on stderr


def test_run_nonexistent_input_exits_3(tmp_path, capsys, monkeypatch):
    for mod in ("labelkit.common.config.loader", "labelkit.common.observability.obslog", "labelkit.common.runtime.llm_client",
                "labelkit.common.runtime.schema_engine", "labelkit.operators.ingest", "labelkit.operators.dedup",
                "labelkit.operators.quality", "labelkit.operators.emitter", "labelkit.orchestration.orchestrator"):
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
    for mod in ("labelkit.common.config.loader", "labelkit.common.observability.obslog", "labelkit.common.runtime.llm_client",
                "labelkit.common.runtime.schema_engine", "labelkit.operators.ingest", "labelkit.operators.dedup",
                "labelkit.operators.quality", "labelkit.operators.emitter", "labelkit.orchestration.orchestrator"):
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
    for mod in ("labelkit.common.config.loader", "labelkit.common.observability.obslog", "labelkit.common.runtime.llm_client",
                "labelkit.common.runtime.schema_engine", "labelkit.operators.ingest", "labelkit.operators.dedup",
                "labelkit.operators.quality", "labelkit.operators.emitter", "labelkit.orchestration.orchestrator"):
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


def test_execute_run_without_a_listener_is_the_pre_v110_path(tmp_path, monkeypatch,
                                                             capsys):
    """v1.10（U19）：``listener`` 是尾参——不传（v1.10 之前的全部调用方）时旁路
    整轮不挂，运行照常收敛退出码（这里走 dry-run，零 LLM 调用）。"""
    for mod in ("labelkit.common.config.loader", "labelkit.operators.ingest",
                "labelkit.orchestration.orchestrator"):
        pytest.importorskip(mod)
    monkeypatch.setenv("LABELKIT_CLI_TEST_KEY", "test-key")
    data = tmp_path / "in.jsonl"
    data.write_text('{"text": "样例"}\n', encoding="utf-8")
    config, project, _ = _write_pair(tmp_path, str(data))

    assert execute_run(config, project, CliOverrides(dry_run=True)) == EXIT_OK
    assert "dry-run" in capsys.readouterr().err


# ── validate --probe（spec §2.4 退出码表 / §7.7「run 之外的面」U13） ──────────
#
# 探针的**可离线部分**：引用集组装（referenced_profiles）× 逐 profile 展开
# （LLMClient.probe_all）× 结果呈现（rich 表 / 逐行 plain）。真连通性属集成面——
# 这里 monkeypatch 的是 Python 对象层的 probe_all 协程，绝不桩 HTTP 传输层。


def _probe(profile: str, *, ok: bool = True, model: str = "glm-5.2",
           latency_ms: int = 421, error: str | None = None,
           key_env: str | None = None) -> ProbeResult:
    """构造一条探测结果（spec 3.9.2 ProbeResult 六字段）。"""
    return ProbeResult(profile=profile, ok=ok, model=model,
                       latency_ms=latency_ms, error=error, key_env=key_env)


def _probe_cfg(**kw) -> ResolvedConfig:
    """引用了三个 llm profile 与一个 embedding profile 的工程配置。"""
    base = dict(
        quality=QualityConfig(mode="pairwise",
                              judges=("default", "judge", "fixer")),
        annotate=AnnotateConfig(enabled=False, instruction="标注"),
        dedup=DedupConfig(semantic=True, semantic_embedding="emb"),
    )
    base.update(kw)
    return _cfg(**base)


def test_probe_referenced_profiles_walks_llm_then_embedding_in_order(monkeypatch):
    """spec §2.4 `validate --probe` / 3.9.2 probe_all（「成本 = 各被引用 profile
    池大小之和 次探测调用」）：探测顺序 = 引用集的 (LLM…, embedding…) 拼接，池化
    profile 的多条结果被**展开**而非嵌套，返回 tuple。"""
    probed: list[str] = []

    async def fake_probe_all(self, name):     # noqa: ANN001 — 对象层桩，非传输层
        probed.append(name)
        if name == "judge":                   # 池化 profile：逐键一条，声明序
            return [_probe(name, key_env="LABELKIT_KEY_A"),
                    _probe(name, ok=False, error="401", key_env="LABELKIT_KEY_B")]
        return [_probe(name)]

    monkeypatch.setattr(LLMClient, "probe_all", fake_probe_all)
    cfg = _probe_cfg()
    results = probe_referenced_profiles(cfg)

    llms, embs = referenced_profiles(cfg)
    assert (llms, embs) == (["default", "judge", "fixer"], ["emb"])
    assert probed == [*llms, *embs]           # 引用集顺序即探测顺序
    assert isinstance(results, tuple)
    assert [r.profile for r in results] == ["default", "judge", "judge",
                                            "fixer", "emb"]
    assert [r.key_env for r in results] == [None, "LABELKIT_KEY_A",
                                            "LABELKIT_KEY_B", None, None]


class _TtyStdout(io.StringIO):
    """stdout 替身：isatty() 为真——U13 的「仅 stdout 为 TTY 时渲染表格」判定面。"""

    def isatty(self) -> bool:
        return True


def _pin_terminal(monkeypatch) -> None:
    """把 rich 的终端探测钉成定宽无色，让表格文本在任何终端下逐字可断言。"""
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm")


_PROBE_RESULTS = (
    _probe("default"),
    _probe("judge", ok=False, model="m", latency_ms=8,
           error="401 unauthorized", key_env="LABELKIT_KEY_B"),
)

_PROBE_LINES = ["probe default: ok model=glm-5.2 latency_ms=421",
                "probe judge[LABELKIT_KEY_B]: FAIL 401 unauthorized"]


def _validate_args(*extra: str):
    """解析一条 validate 命令行（真 argparse 面，不手搓 Namespace）。"""
    return cli.build_parser().parse_args(
        ["validate", "--config", "c.toml", "--project", "p.toml", *extra])


def test_validate_probe_prints_one_line_per_result_when_not_rich_tty(
        monkeypatch, capsys):
    """U13：非 rich 档保持行式输出（脚本消费不破坏）——`probe {label}: ok
    model=… latency_ms=…` / `probe {label}: FAIL {error}`，label 在有 key_env
    时为 `profile[key_env]`；退出码 0。"""
    seen: list = []
    monkeypatch.setattr(cli_commands, "validate_project",
                        lambda *a, **kw: seen.append(kw["overrides"]) or _cfg())
    monkeypatch.setattr(cli_commands, "probe_referenced_profiles",
                        lambda cfg: _PROBE_RESULTS)

    assert cli_commands._cmd_validate(_validate_args("--probe")) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.splitlines() == _PROBE_LINES
    assert "configuration valid" in captured.err
    assert seen[0].console is None            # U27：--console 覆盖项照常透传


def test_validate_probe_renders_the_rich_table_when_rich_and_tty(monkeypatch):
    """U13：rich 档 × stdout 为 TTY 走表格，并**提前返回**——同一批结果不再逐行
    重复打印一遍。"""
    _pin_terminal(monkeypatch)
    monkeypatch.setattr(cli_commands, "validate_project", lambda *a, **kw: _cfg(
        console=ConsoleConfig(mode="rich", mode_resolved="rich")))
    monkeypatch.setattr(cli_commands, "probe_referenced_profiles",
                        lambda cfg: _PROBE_RESULTS)
    fake_out = _TtyStdout()
    monkeypatch.setattr(sys, "stdout", fake_out)

    assert cli_commands._cmd_validate(_validate_args("--probe")) == EXIT_OK
    text = fake_out.getvalue()
    assert "validate --probe" in text
    assert "judge[LABELKIT_KEY_B]" in text and "FAIL" in text
    for line in _PROBE_LINES:
        assert line not in text               # 表格分支 return EXIT_OK 提前返回


def test_validate_probe_falls_back_to_lines_when_rich_is_unimportable(monkeypatch):
    """U21：mode_resolved 只做过 find_spec 探测——rich 实际装坏时表格返回 False，
    调用方回落逐行 plain（退出码不变）。"""
    monkeypatch.setattr(cli_commands, "validate_project", lambda *a, **kw: _cfg(
        console=ConsoleConfig(mode="rich", mode_resolved="rich")))
    monkeypatch.setattr(cli_commands, "probe_referenced_profiles",
                        lambda cfg: _PROBE_RESULTS)
    monkeypatch.setitem(sys.modules, "rich.table", None)
    fake_out = _TtyStdout()
    monkeypatch.setattr(sys, "stdout", fake_out)

    assert cli_commands._cmd_validate(_validate_args("--probe")) == EXIT_OK
    assert fake_out.getvalue().splitlines() == _PROBE_LINES


def test_validate_without_probe_makes_no_probe_calls(monkeypatch, capsys):
    """无 `--probe` 时只做 M1 全量校验：零探测调用、stdout 一字不出。"""
    monkeypatch.setattr(cli_commands, "validate_project", lambda *a, **kw: _cfg())
    monkeypatch.setattr(cli_commands, "probe_referenced_profiles",
                        lambda cfg: pytest.fail("probe must not run"))

    assert cli_commands._cmd_validate(_validate_args()) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "configuration valid" in captured.err


def test_print_probe_table_renders_five_columns(monkeypatch):
    """U13 探测表：五列表头齐全、ok/FAIL 两态、latency_ms 右对齐。

    表头 `profile[key]` 与 `profile[key_env]` 标签都带方括号——rich 的控制台
    markup 会把小写方括号片段当样式标签吃掉，故生产侧以 Text 直传
    （裁决·探测表字面格以 Text 直传）；本例正是那条回归锚。"""
    _pin_terminal(monkeypatch)
    fake_out = _TtyStdout()
    monkeypatch.setattr(sys, "stdout", fake_out)

    assert cli_commands._print_probe_table(_PROBE_RESULTS) is True
    lines = [ln for ln in fake_out.getvalue().splitlines() if ln.strip()]
    header = next(ln for ln in lines if "status" in ln)
    for column in ("profile[key]", "status", "model", "latency_ms", "error"):
        assert column in header, column
    ok_row = next(ln for ln in lines if "default" in ln and "ok" in ln)
    fail_row = next(ln for ln in lines if "judge[LABELKIT_KEY_B]" in ln)
    assert "glm-5.2" in ok_row and "421" in ok_row
    assert "FAIL" in fail_row and "401 unauthorized" in fail_row
    # 右对齐：latency_ms 值贴着本格右边界（后面只剩边框与空格）
    assert ok_row.index("421") > ok_row.index("glm-5.2")
    assert ok_row[ok_row.index("421") + 3:].lstrip().startswith("│")


def test_print_probe_table_returns_false_when_rich_is_unimportable(monkeypatch,
                                                                   capsys):
    """rich 不可导入 ⇒ 返回 False 且**零 stdout 输出**（调用方据此回落逐行）。"""
    monkeypatch.setitem(sys.modules, "rich.table", None)
    assert cli_commands._print_probe_table(_PROBE_RESULTS) is False
    assert capsys.readouterr().out == ""


# ── main() 的兜底异常出口与 __main__ 守卫（spec §2.4 退出码 / §4.3 映射） ─────


def _raise_from_rubric(monkeypatch, exc: BaseException) -> None:
    """让 rubric 处理器抛出指定异常（main 的 handlers 表按模块全局名查找）。"""
    def boom(_args):
        raise exc

    monkeypatch.setattr(cli_main, "_cmd_rubric", boom)


@pytest.fixture
def _isolated_cli_logging():
    """摘掉 setup_logging 留在进程里的 handler。

    它是**每进程一次**的装配（生产里一次运行一个进程），但在同一个 pytest 进程内
    会绑着上一条用例早已关闭的捕获流——写失败时 logging 会把自己的报错倾泻到当前
    stderr，淹没本例要逐行断言的 CLI 输出。
    """
    logger = logging.getLogger("labelkit")
    saved = logger.handlers[:]
    logger.handlers = []
    try:
        yield
    finally:
        logger.handlers = saved


def test_main_keyboard_interrupt_prints_interrupted_and_exits_4(
        monkeypatch, capsys, caplog, _isolated_cli_logging):
    """Ctrl-C：stderr 打 `interrupted`、退出码 4、恰一条分类 error 日志，
    且**不重新抛出**（进程以退出码而非 traceback 收尾）。"""
    _raise_from_rubric(monkeypatch, KeyboardInterrupt())
    with caplog.at_level(logging.ERROR, logger="labelkit.cli"):
        rc = cli.main(["rubric"])
    assert rc == EXIT_FATAL
    assert capsys.readouterr().err.splitlines() == ["interrupted"]
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    assert "interrupted by user" in records[0].getMessage()


def test_main_unexpected_exception_maps_through_exit_code_for(
        monkeypatch, capsys, caplog, _isolated_cli_logging):
    """兜底 Exception 分支：退出码取 `exit_code_for(exc)`、走 `_print_exception`
    的单行形，同样不外抛。"""
    exc = RuntimeError("unexpected")
    _raise_from_rubric(monkeypatch, exc)
    with caplog.at_level(logging.ERROR, logger="labelkit.cli"):
        rc = cli.main(["rubric"])
    assert rc == cli.exit_code_for(exc) == EXIT_FATAL
    assert capsys.readouterr().err.splitlines() == ["RuntimeError: unexpected"]
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    assert "command rubric failed: RuntimeError (exit 4)" in records[0].getMessage()


@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_main_module_guard_exits_with_the_command_code(monkeypatch, capsys):
    """`__main__` 守卫：以脚本形态执行时 `sys.exit(main())` 把子命令退出码交给
    进程（spec §2.4 退出码表的进程边界）。"""
    monkeypatch.setattr(sys, "argv", ["labelkit", "rubric"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("labelkit.cli.main", run_name="__main__")
    assert excinfo.value.code == EXIT_OK
    assert set(capsys.readouterr().out.splitlines()) == {
        "default:text", "default:ui", "default:trajectory"}

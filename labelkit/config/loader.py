"""M1 config loader (spec 3.1, CONTRACTS.md §6.2/§6.3).

load(): three-source merge — CLI overrides > project.toml > config.toml/built-in
defaults — plus FULL startup validation. Every validation error is aggregated into
a single ConfigError (never first-error-only); unknown keys produce stderr
warnings only (forward compatibility).

default_rubric(): loads a packaged default rubric from labelkit/data/rubrics/.

Error message format (spec 3.1.5): "<file>:[section].key: <expected>, got <actual>"
with a machine-stable "<file>:[section].key:" prefix and Chinese message bodies;
array-table elements are addressed as "[[section.key]][N]" with N 1-based.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import replace
from importlib import resources
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from labelkit.config.model import (
    AnnotateConfig,
    CliOverrides,
    Criterion,
    DedupConfig,
    EmbeddingProfile,
    FewShotExample,
    GenerateConfig,
    GenerateStyle,
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
from labelkit.errors import ConfigError
from labelkit.hooks import normalize_violations, resolve_hook

__all__ = ["load", "default_rubric"]

_MISSING = object()

_KEY_RE = re.compile(r"[a-z0-9_]+")

_RUBRIC_PKG_FILES: dict[str, str] = {
    "default:text": "default_text.toml",
    "default:ui": "default_ui.toml",
}

_TRACE_CHANNELS = ("ingest", "dedup", "quality", "annotate", "verify", "schema", "llm")


# ── low-level helpers ──────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    """Render an offending value the way the spec samples do (JSON-style)."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


class _Collector:
    """Aggregates every error/warning across the whole load (spec 3.1.5)."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


class _Tbl:
    """Typed reader over one TOML table; records errors, falls back to defaults."""

    def __init__(self, col: _Collector, file: str, label: str, data: Any) -> None:
        self.col = col
        self.file = file
        self.label = label                      # "[run]", "[llm.default]", "" for top level
        self.data: dict = data if isinstance(data, dict) else {}
        self.seen: set[str] = set()

    def loc(self, key: str) -> str:
        return f"{self.file}:{self.label}.{key}" if self.label else f"{self.file}:{key}"

    def err(self, key: str, expected: str, got: Any = _MISSING) -> None:
        if got is _MISSING:
            self.col.error(f"{self.loc(key)}: 缺失必填键，期望{expected}")
        else:
            self.col.error(f"{self.loc(key)}: 期望{expected}，得到 {_fmt(got)}")

    def take(self, key: str) -> Any:
        self.seen.add(key)
        return self.data.get(key, _MISSING)

    # typed getters — on any violation the error is recorded and `default` returned

    def get_str(self, key: str, default: Any = None, *, required: bool = False,
                enum: tuple[str, ...] | None = None, nonempty: bool = False) -> Any:
        if enum is not None:
            expected = " | ".join(json.dumps(e) for e in enum)
            expected = f" {expected}"
        elif nonempty:
            expected = "非空字符串"
        else:
            expected = "字符串"
        v = self.take(key)
        if v is _MISSING:
            if required:
                self.err(key, expected)
            return default
        if not isinstance(v, str):
            self.err(key, expected, v)
            return default
        if enum is not None and v not in enum:
            self.err(key, expected, v)
            return default
        if nonempty and not v.strip():
            self.err(key, "非空字符串", v)
            return default
        return v

    def get_int(self, key: str, default: Any = None, *, required: bool = False,
                minimum: int | None = None) -> Any:
        if minimum == 1:
            expected = "正整数"
        elif minimum == 0:
            expected = "非负整数"
        else:
            expected = "整数"
        v = self.take(key)
        if v is _MISSING:
            if required:
                self.err(key, expected)
            return default
        if isinstance(v, bool) or not isinstance(v, int) or (minimum is not None and v < minimum):
            self.err(key, expected, v)
            return default
        return v

    def get_float(self, key: str, default: Any = None, *, required: bool = False,
                  gt: float | None = None, ge: float | None = None,
                  le: float | None = None) -> Any:
        if gt == 0 and le == 1:
            expected = "(0,1] 内的数值"
        elif ge == 0 and le == 1:
            expected = "[0,1] 内的数值"
        elif gt == 0:
            expected = "正数"
        elif ge == 0:
            expected = "非负数值"
        else:
            expected = "数值"
        v = self.take(key)
        if v is _MISSING:
            if required:
                self.err(key, expected)
            return default
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            self.err(key, expected, v)
            return default
        f = float(v)
        if (gt is not None and not f > gt) or (ge is not None and not f >= ge) \
                or (le is not None and not f <= le):
            self.err(key, expected, v)
            return default
        return f

    def get_bool(self, key: str, default: Any = None, *, required: bool = False) -> Any:
        v = self.take(key)
        if v is _MISSING:
            if required:
                self.err(key, "布尔值")
            return default
        if not isinstance(v, bool):
            self.err(key, "布尔值", v)
            return default
        return v

    def get_str_tuple(self, key: str, default: tuple = (), *,
                      elem_enum: tuple[str, ...] | None = None) -> tuple:
        v = self.take(key)
        if v is _MISSING:
            return default
        if not isinstance(v, list):
            self.err(key, "字符串数组", v)
            return default
        out: list[str] = []
        ok = True
        for i, e in enumerate(v, 1):
            if not isinstance(e, str):
                self.col.error(f"{self.loc(key)}[{i}]: 期望字符串，得到 {_fmt(e)}")
                ok = False
            elif elem_enum is not None and e not in elem_enum:
                allowed = " | ".join(json.dumps(x) for x in elem_enum)
                self.col.error(f"{self.loc(key)}[{i}]: 期望 {allowed}，得到 {_fmt(e)}")
                ok = False
            else:
                out.append(e)
        return tuple(out) if ok else default

    def get_float_tuple(self, key: str, default: tuple = ()) -> tuple:
        v = self.take(key)
        if v is _MISSING:
            return default
        if not isinstance(v, list):
            self.err(key, "数值数组", v)
            return default
        out: list[float] = []
        for i, e in enumerate(v, 1):
            if isinstance(e, bool) or not isinstance(e, (int, float)):
                self.col.error(f"{self.loc(key)}[{i}]: 期望数值，得到 {_fmt(e)}")
                return default
            out.append(float(e))
        return tuple(out)

    def finish(self) -> None:
        """Warn on unknown keys (forward compatibility — never an error)."""
        for k in self.data:
            if k not in self.seen:
                self.col.warn(f"{self.loc(k)}: 未知键，已忽略（前向兼容）")


def _section(col: _Collector, top: _Tbl, key: str) -> Any:
    """Take a top-level table; absent → None (defaults apply); wrong type → error."""
    v = top.take(key)
    if v is _MISSING:
        return None
    if not isinstance(v, dict):
        col.error(f"{top.file}:{key}: 期望表（table），得到 {_fmt(v)}")
        return None
    return v


def _check_schema_version(col: _Collector, top: _Tbl) -> None:
    v = top.take("schema_version")
    if v is _MISSING:
        col.error(f"{top.file}:schema_version: 缺失必填键，期望 1")
    elif isinstance(v, bool) or not isinstance(v, int) or v != 1:
        col.error(f"{top.file}:schema_version: 期望 1，得到 {_fmt(v)}")


# ── config.toml side ───────────────────────────────────────────────────────


def _parse_tool(col: _Collector, file: str, data: Any) -> ToolConfig:
    t = _Tbl(col, file, "[tool]", data)
    tool = ToolConfig(
        log_level=t.get_str("log_level", "info", enum=("debug", "info", "warn", "error")),
        log_format=t.get_str("log_format", "text", enum=("text", "jsonl")),
    )
    t.finish()
    return tool


def _parse_llm_profile(col: _Collector, file: str, name: str, data: dict) -> LLMProfile:
    t = _Tbl(col, file, f"[llm.{name}]", data)
    prof = LLMProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", required=True,
                           enum=("openai_compatible", "anthropic")),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=t.get_str("api_key_env", "", required=True, nonempty=True) or "",
        max_concurrency=t.get_int("max_concurrency", 8, minimum=1),
        timeout_s=t.get_int("timeout_s", 120, minimum=1),
        max_retries=t.get_int("max_retries", 5, minimum=0),
        retry_base_delay_s=t.get_float("retry_base_delay_s", 1.0, gt=0),
        supports_structured_output=t.get_bool("supports_structured_output", False),
        supports_vision=t.get_bool("supports_vision", False),
        max_output_tokens=t.get_int("max_output_tokens", 4096, minimum=1),
        temperature=t.get_float("temperature", 0.0, ge=0),
        max_image_px=t.get_int("max_image_px", 2048, minimum=1),
        price_per_mtok_in=t.get_float("price_per_mtok_in", None, ge=0),
        price_per_mtok_out=t.get_float("price_per_mtok_out", None, ge=0),
    )
    t.finish()
    return prof


def _parse_embedding_profile(col: _Collector, file: str, name: str, data: dict) -> EmbeddingProfile:
    t = _Tbl(col, file, f"[embedding.{name}]", data)
    prof = EmbeddingProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", enum=("openai_compatible",)),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=t.get_str("api_key_env", "", required=True, nonempty=True) or "",
        max_concurrency=t.get_int("max_concurrency", 8, minimum=1),
        timeout_s=t.get_int("timeout_s", 60, minimum=1),
        max_retries=t.get_int("max_retries", 5, minimum=0),
        retry_base_delay_s=t.get_float("retry_base_delay_s", 1.0, gt=0),
        dims=t.get_int("dims", None, minimum=1),
    )
    t.finish()
    return prof


def _parse_config_file(col: _Collector, file: str, data: dict) -> tuple[
        ToolConfig, dict[str, LLMProfile], dict[str, EmbeddingProfile]]:
    top = _Tbl(col, file, "", data)
    _check_schema_version(col, top)
    tool = _parse_tool(col, file, _section(col, top, "tool"))

    llm_profiles: dict[str, LLMProfile] = {}
    llm_data = top.take("llm")
    if llm_data is _MISSING or not isinstance(llm_data, dict) or not llm_data:
        col.error(f"{file}:llm: 至少需要 1 个 [llm.<name>] profile")
    else:
        for name, sub in llm_data.items():
            if not isinstance(sub, dict):
                col.error(f"{file}:[llm.{name}]: 期望表（table），得到 {_fmt(sub)}")
                continue
            llm_profiles[name] = _parse_llm_profile(col, file, name, sub)

    embedding_profiles: dict[str, EmbeddingProfile] = {}
    emb_data = top.take("embedding")
    if emb_data is not _MISSING:
        if not isinstance(emb_data, dict):
            col.error(f"{file}:embedding: 期望表（table），得到 {_fmt(emb_data)}")
        else:
            for name, sub in emb_data.items():
                if not isinstance(sub, dict):
                    col.error(f"{file}:[embedding.{name}]: 期望表（table），得到 {_fmt(sub)}")
                    continue
                embedding_profiles[name] = _parse_embedding_profile(col, file, name, sub)

    top.finish()
    return tool, llm_profiles, embedding_profiles


# ── project.toml side ──────────────────────────────────────────────────────


def _parse_criteria(col: _Collector, file: str, raw: Any,
                    label: str = "rubric.criteria") -> tuple[Criterion, ...]:
    """Parse a [[<label>]] array of tables. Enforces key pattern/uniqueness,
    required fields and weight > 0 (spec 3.1.4 rubric row, locatable errors)."""
    if not isinstance(raw, list):
        col.error(f"{file}:[[{label}]]: 期望表数组，得到 {_fmt(raw)}")
        return ()
    criteria: list[Criterion] = []
    seen_keys: set[str] = set()
    for i, sub in enumerate(raw, 1):
        elem_label = f"[[{label}]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{elem_label}: 期望表（table），得到 {_fmt(sub)}")
            continue
        t = _Tbl(col, file, elem_label, sub)
        key = t.get_str("key", None, required=True, nonempty=True)
        if key is not None and not _KEY_RE.fullmatch(key):
            col.error(f"{file}:{elem_label}.key: 期望匹配 [a-z0-9_]+，得到 {_fmt(key)}")
            key = None
        if key is not None:
            if key in seen_keys:
                col.error(f"{file}:{elem_label}.key: key 须唯一，得到重复的 {_fmt(key)}")
            seen_keys.add(key)
        description = t.get_str("description", "", required=True, nonempty=True) or ""
        pairwise_prompt = t.get_str("pairwise_prompt", "", required=True, nonempty=True) or ""
        weight = t.get_float("weight", 1.0, gt=0)
        pointwise_levels = t.get_str_tuple("pointwise_levels", ())
        t.finish()
        criteria.append(Criterion(
            key=key or f"criterion_{i}",
            description=description,
            pairwise_prompt=pairwise_prompt,
            weight=weight,
            pointwise_levels=pointwise_levels,
        ))
    return tuple(criteria)


def _parse_styles(col: _Collector, file: str, raw: Any) -> tuple[GenerateStyle, ...]:
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[generate].styles: 期望表数组，得到 {_fmt(raw)}")
        return ()
    styles: list[GenerateStyle] = []
    seen: set[str] = set()
    for i, sub in enumerate(raw, 1):
        label = f"[[generate.styles]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: 期望表（table），得到 {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        name = t.get_str("name", None, required=True, nonempty=True)
        prompt = t.get_str("prompt", None, required=True, nonempty=True)
        t.finish()
        if name is not None:
            if name in seen:
                col.error(f"{file}:{label}.name: 表内 name 须唯一，得到重复的 {_fmt(name)}")
            seen.add(name)
        if name is not None and prompt is not None:
            styles.append(GenerateStyle(name=name, prompt=prompt))
    return tuple(styles)


def _parse_examples(col: _Collector, file: str, raw: Any) -> tuple[FewShotExample, ...]:
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[annotate].examples: 期望表数组，得到 {_fmt(raw)}")
        return ()
    examples: list[FewShotExample] = []
    for i, sub in enumerate(raw, 1):
        label = f"[[annotate.examples]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: 期望表（table），得到 {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        inp = t.get_str("input", None, required=True, nonempty=True)
        out = t.take("output")
        if out is _MISSING:
            t.err("output", "表（对象，须通过用户 Schema）")
            out = None
        elif not isinstance(out, dict):
            t.err("output", "表（对象，须通过用户 Schema）", out)
            out = None
        t.finish()
        if inp is not None and out is not None:
            examples.append(FewShotExample(input=inp, output=out))
    return tuple(examples)


def _parse_judgment_reasons(col: _Collector, t: _Tbl) -> bool | str:
    v = t.take("judgment_reasons")
    if v is _MISSING:
        return "auto"
    if isinstance(v, bool) or v == "auto":
        return v
    t.col.error(f'{t.loc("judgment_reasons")}: 期望 "auto" | true | false，得到 {_fmt(v)}')
    return "auto"


def _parse_project_file(col: _Collector, file: str, data: dict) -> dict[str, Any]:
    top = _Tbl(col, file, "", data)
    _check_schema_version(col, top)

    t = _Tbl(col, file, "[run]", _section(col, top, "run"))
    run = dict(
        input=t.get_str("input", None, nonempty=True),
        output=t.get_str("output", None, nonempty=True),
        modality=t.get_str("modality", None, required=True, enum=("text", "ui")),
        mode=t.get_str("mode", "process", enum=("process", "generate_only")),
        batch_size=t.get_int("batch_size", 256, minimum=1),
        seed=t.get_int("seed", 0),
        fatal_error_threshold=t.get_int("fatal_error_threshold", 20, minimum=1),
    )
    t.finish()

    t = _Tbl(col, file, "[input]", _section(col, top, "input"))
    input_cfg = InputConfig(
        text_field=t.get_str("text_field", "text", nonempty=True),
        on_bad_line=t.get_str("on_bad_line", "skip", enum=("skip", "fail")),
        on_missing_pair=t.get_str("on_missing_pair", "skip", enum=("skip", "fail")),
        on_index_conflict=t.get_str("on_index_conflict", "fail", enum=("skip", "fail")),
        max_image_mb=t.get_int("max_image_mb", 20, minimum=1),
        ui_tree_max_chars=t.get_int("ui_tree_max_chars", 30000, minimum=1),
    )
    t.finish()

    t = _Tbl(col, file, "[dedup]", _section(col, top, "dedup"))
    dedup = DedupConfig(
        enabled=t.get_bool("enabled", True),
        scope=t.get_str("scope", "global", enum=("global", "batch")),
        minhash_threshold=t.get_float("minhash_threshold", 0.85, gt=0, le=1),
        minhash_num_perm=t.get_int("minhash_num_perm", 128, minimum=1),
        ngram=t.get_int("ngram", 5, minimum=1),
        image_phash_max_distance=t.get_int("image_phash_max_distance", 8, minimum=0),
        ui_dup_requires=t.get_str("ui_dup_requires", "both", enum=("both", "tree", "image")),
        bounds_quantize_px=t.get_int("bounds_quantize_px", 4, minimum=0),
        semantic=t.get_bool("semantic", False),
        semantic_embedding=t.get_str("semantic_embedding", None, nonempty=True),
        semantic_threshold=t.get_float("semantic_threshold", 0.95, gt=0, le=1),
    )
    t.finish()

    quality_section = _section(col, top, "quality")
    t = _Tbl(col, file, "[quality]", quality_section)
    quality = QualityConfig(
        enabled=t.get_bool("enabled", True),
        mode=t.get_str("mode", "pairwise", enum=("pairwise", "pointwise")),
        llm=t.get_str("llm", "default", nonempty=True),
        rounds=t.get_int("rounds", 4, minimum=1),
        criteria_per_call=t.get_str("criteria_per_call", "all", enum=("all", "single")),
        threshold=t.get_float("threshold", None, ge=0, le=1),
        selection=t.get_str("selection", "threshold", enum=("threshold", "top_ratio")),
        top_ratio=t.get_float("top_ratio", None, gt=0, le=1),
        judges=t.get_str_tuple("judges", ()),
        both_orders=t.get_bool("both_orders", False),
        on_unscored=t.get_str("on_unscored", "keep", enum=("keep", "drop")),
        rubric=t.get_str("rubric", "", enum=("default:text", "default:ui", "inline")) or "",
        judgment_reasons=_parse_judgment_reasons(col, t),
    )
    t.finish()

    gen_section = _section(col, top, "generate")
    t = _Tbl(col, file, "[generate]", gen_section)
    generate = GenerateConfig(
        enabled=t.get_bool("enabled", False),
        llms=t.get_str_tuple("llms", ("default",)) or ("default",),
        instruction=t.get_str("instruction", "") or "",
        mixture=t.get_str("mixture", "round_robin", enum=("round_robin", "weighted")),
        weights=t.get_float_tuple("weights", ()),
        styles=_parse_styles(col, file, t.take("styles")),
        num_per_record=t.get_int("num_per_record", 2, minimum=1),
        seeds_per_call=t.get_int("seeds_per_call", 3, minimum=1),
        num_per_call=t.get_int("num_per_call", 4, minimum=1),
        seed_min_score=t.get_float("seed_min_score", None, ge=0, le=1),
        temperature=t.get_float("temperature", 0.9, ge=0),
        sample_validator=t.get_str("sample_validator", None, nonempty=True),
        seed_examples=t.get_str_tuple("seed_examples", ()),
        standalone_count=t.get_int("standalone_count", None, minimum=1),
    )
    # distinguish "explicitly set" from "dataclass default" for the mode rules
    gen_provided = {
        "seed_examples": isinstance(gen_section, dict) and "seed_examples" in gen_section,
        "standalone_count": isinstance(gen_section, dict) and "standalone_count" in gen_section,
    }
    top_ratio_provided = isinstance(quality_section, dict) and "top_ratio" in quality_section
    t.finish()

    t = _Tbl(col, file, "[annotate]", _section(col, top, "annotate"))
    annotate = AnnotateConfig(
        enabled=t.get_bool("enabled", True),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        examples=_parse_examples(col, file, t.take("examples")),
        self_consistency=t.get_int("self_consistency", 0, minimum=0),
        sc_temperature=t.get_float("sc_temperature", 0.7, ge=0),
    )
    t.finish()

    t = _Tbl(col, file, "[verify]", _section(col, top, "verify"))
    verify = VerifyConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "judge", nonempty=True),
        judges=t.get_str_tuple("judges", ()),
        policy=t.get_str("policy", "drop", enum=("drop", "repair")),
        max_repair_rounds=t.get_int("max_repair_rounds", 1, minimum=0),
        extra_criteria=t.get_str("extra_criteria", "") or "",
    )
    t.finish()

    t = _Tbl(col, file, "[output]", _section(col, top, "output"))
    output = OutputConfig(
        schema_path=t.get_str("schema_path", None, nonempty=True),
        schema_inline=t.get_str("schema_inline", None, nonempty=True),
        max_repair_attempts=t.get_int("max_repair_attempts", 2, minimum=0),
        repair_llm=t.get_str("repair_llm", None, nonempty=True),
        meta_mode=t.get_str("meta_mode", "inline", enum=("inline", "sidecar", "none")),
        passthrough_fields=t.get_str_tuple("passthrough_fields", ()),
        rejects=t.get_str("rejects", "refs", enum=("none", "refs", "full")),
        validator=t.get_str("validator", None, nonempty=True),
    )
    t.finish()

    t = _Tbl(col, file, "[trace]", _section(col, top, "trace"))
    trace = TraceConfig(
        enabled=t.get_bool("enabled", False),
        path=t.get_str("path", "") or "",
        channels=t.get_str_tuple("channels", ("quality", "verify", "schema"),
                                 elem_enum=_TRACE_CHANNELS),
        content=t.get_str("content", "refs", enum=("none", "refs", "excerpt", "full")),
    )
    t.finish()

    # [rubric] is NOT parsed here: rubric errors must be reported in the rubric
    # slot of the 3.1.4 table-row order (spec 3.1.5 sample output), so load()
    # parses it lazily during rubric resolution.
    rubric_raw = _section(col, top, "rubric")

    top.finish()
    return dict(
        run=run, input=input_cfg, dedup=dedup, quality=quality, generate=generate,
        gen_provided=gen_provided, top_ratio_provided=top_ratio_provided,
        annotate=annotate, verify=verify, output=output,
        trace=trace, rubric_raw=rubric_raw,
    )


# ── user schema ────────────────────────────────────────────────────────────


# Keyword positions whose values are DATA, not subschemas — a "$ref"-shaped string
# inside them is literal content and must not be resolution-checked.
_SCHEMA_DATA_KEYS = frozenset({"const", "enum", "default", "examples"})


def _collect_schema_refs(node: Any, base: str,
                         out: list[tuple[str, str]]) -> None:
    """Walk the schema document collecting (base_uri, $ref) pairs, tracking nested
    `$id` base-URI changes (RFC 3986 join) and skipping data positions."""
    if isinstance(node, dict):
        nid = node.get("$id")
        if isinstance(nid, str) and nid:
            base = urljoin(base, nid)
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append((base, ref))
        for k, v in node.items():
            if k in _SCHEMA_DATA_KEYS:
                continue
            _collect_schema_refs(v, base, out)
    elif isinstance(node, list):
        for v in node:
            _collect_schema_refs(v, base, out)


def _unresolvable_refs(schema: dict) -> list[tuple[str, str]]:
    """CONTRACTS §6.3 rule 13 ($ref resolvability, §12 #23): every `$ref` must resolve
    against the schema document itself — the tool never retrieves external resources at
    runtime, so a ref that fails here is guaranteed to blow up M8 validation on every
    record (spec 3.1 M1 contract: 不存在运行期配置错误). Returns [(ref, reason)] deduped
    by ref, deterministically ordered. Best-effort: if the referencing machinery itself
    cannot ingest the document, returns [] (the rule-15 runtime guard still backstops)."""
    try:
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        root_uri = resource.id() or ""
        registry = Registry().with_resource(root_uri, resource).crawl()
    except Exception:
        return []
    pairs: list[tuple[str, str]] = []
    _collect_schema_refs(schema, root_uri, pairs)
    bad: dict[str, str] = {}
    for base, ref in pairs:
        if ref in bad:
            continue
        try:
            registry.resolver(base).lookup(ref)
        except Exception as e:
            bad[ref] = str(e)
    return sorted(bad.items())


def _load_user_schema(col: _Collector, file: str, output: OutputConfig) -> tuple[dict, bool]:
    """Rules 13/14 of CONTRACTS §6.3. Returns (schema_dict, usable)."""
    sp, si = output.schema_path, output.schema_inline
    if sp is not None and si is not None:
        col.error(f"{file}:[output].schema_inline: 与 schema_path 恰好提供其一（互斥），得到两者均设置")
        return {}, False
    if sp is None and si is None:
        col.error(f"{file}:[output].schema_path: 须恰好提供 schema_path 或 schema_inline 其一，得到两者均缺失")
        return {}, False
    key = "schema_inline" if si is not None else "schema_path"
    text = si
    if sp is not None:
        try:
            text = Path(sp).read_text(encoding="utf-8")
        except OSError as e:
            col.error(f"{file}:[output].schema_path: 无法读取 Schema 文件 {_fmt(sp)}：{e}")
            return {}, False
    try:
        schema = json.loads(text)  # type: ignore[arg-type]
    except json.JSONDecodeError as e:
        col.error(f"{file}:[output].{key}: 期望合法 JSON，得到 JSON 解析错误：{e}")
        return {}, False
    if not isinstance(schema, dict):
        col.error(f"{file}:[output].{key}: 用户 Schema 顶层必须为 JSON 对象，得到 {_fmt(schema)}")
        return {}, False
    ok = True
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        col.error(f"{file}:[output].{key}: 未通过 JSON Schema draft 2020-12 元 Schema 校验：{e.message}")
        ok = False
    if schema.get("type") != "object":
        col.error(f'{file}:[output].{key}: 用户 Schema 顶层 type 必须为 "object"，'
                  f"得到 {_fmt(schema.get('type'))}")
        ok = False
    props = schema.get("properties")
    if isinstance(props, dict) and "_meta" in props:
        col.error(f'{file}:[output].{key}: 用户 Schema 顶层不得声明保留键 "_meta"'
                  f'（6.3 信封字段由工具写入），得到 properties 含 "_meta"')
        ok = False
    if ok:
        for ref, why in _unresolvable_refs(schema):
            col.error(f"{file}:[output].{key}: 用户 Schema 引用无法解析"
                      f"（$ref {_fmt(ref)}）：{why}")
            ok = False
    return schema, ok


# ── public API ─────────────────────────────────────────────────────────────


def default_rubric(name: Literal["default:text", "default:ui"]) -> Rubric:
    """Load a packaged default rubric from labelkit/data/rubrics/*.toml
    (importlib.resources)."""
    try:
        fname = _RUBRIC_PKG_FILES[name]
    except KeyError:
        raise ValueError(
            f'unknown default rubric {name!r}; expected "default:text" or "default:ui"'
        ) from None
    text = (resources.files("labelkit") / "data" / "rubrics" / fname).read_text(encoding="utf-8")
    data = tomllib.loads(text)
    criteria = tuple(
        Criterion(
            key=c["key"],
            description=c["description"],
            pairwise_prompt=c["pairwise_prompt"],
            weight=float(c.get("weight", 1.0)),
            pointwise_levels=tuple(c.get("pointwise_levels", ())),
        )
        for c in data.get("criteria", ())
    )
    return Rubric(name=data["name"], criteria=criteria)


def load(config_path: Path, project_path: Path,
         cli_overrides: CliOverrides) -> ResolvedConfig:
    """Three-source merge + full validation. On failure raises ConfigError(errors: list[str])
    carrying ALL errors (never first-only); CLI exits 2."""
    col = _Collector()
    cli = cli_overrides
    fc, fp = str(config_path), str(project_path)

    # ── read + parse both files (best-effort; aggregate) ──────────────────
    def _read(path: Path, label: str) -> tuple[bytes | None, dict | None]:
        try:
            raw = Path(path).read_bytes()
        except OSError as e:
            col.error(f"{label}: 无法读取配置文件：{e}")
            return None, None
        try:
            return raw, tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            col.error(f"{label}: TOML 解析失败：{e}")
            return raw, None

    config_raw, config_data = _read(Path(config_path), fc)
    project_raw, project_data = _read(Path(project_path), fp)
    config_ok = config_data is not None
    project_ok = project_data is not None

    tool = ToolConfig()
    llm_profiles: dict[str, LLMProfile] = {}
    embedding_profiles: dict[str, EmbeddingProfile] = {}
    if config_ok:
        tool, llm_profiles, embedding_profiles = _parse_config_file(col, fc, config_data)

    if project_ok:
        p = _parse_project_file(col, fp, project_data)
    else:
        p = None

    if p is None:
        _flush_warnings(col)
        raise ConfigError(col.errors or [f"{fp}: 配置装载失败"])

    run: dict[str, Any] = p["run"]
    input_cfg: InputConfig = p["input"]
    dedup: DedupConfig = p["dedup"]
    quality: QualityConfig = p["quality"]
    generate: GenerateConfig = p["generate"]
    gen_provided: dict[str, bool] = p["gen_provided"]
    annotate: AnnotateConfig = p["annotate"]
    verify: VerifyConfig = p["verify"]
    output: OutputConfig = p["output"]
    trace: TraceConfig = p["trace"]
    rubric_raw: Any = p["rubric_raw"]

    modality: str = run["modality"] or "text"
    mode: str = run["mode"] or "process"

    if cli.log_level is not None and cli.log_level not in ("debug", "info", "warn", "error"):
        col.error(f'cli:--log-level: 期望 "debug" | "info" | "warn" | "error"，'
                  f"得到 {_fmt(cli.log_level)}")

    # ── rule 2/3/4/5 — profile references (§6.3) ──────────────────────────
    def _check_llm_ref(loc: str, name: str) -> None:
        if config_ok and name and name not in llm_profiles:
            avail = "、".join(llm_profiles) if llm_profiles else "（无）"
            col.error(f"{loc}: 引用的 profile {_fmt(name)} 不存在于 config.toml [llm.*]，"
                      f"可用：{avail}")

    _check_llm_ref(f"{fp}:[quality].llm", quality.llm)
    _check_llm_ref(f"{fp}:[annotate].llm", annotate.llm)
    for i, name in enumerate(generate.llms, 1):
        _check_llm_ref(f"{fp}:[generate].llms[{i}]", name)
    if verify.enabled and not verify.judges:
        # spec §5.2 footnote †: default "judge" not required when disabled; a
        # non-empty judges panel REPLACES verify.llm at runtime (3.7.2), so its
        # existence is not required either (E2E finding P3-8) — the panel
        # members themselves are checked below.
        _check_llm_ref(f"{fp}:[verify].llm", verify.llm)
    if output.repair_llm is not None:
        _check_llm_ref(f"{fp}:[output].repair_llm", output.repair_llm)
    for section, judges in (("quality", quality.judges), ("verify", verify.judges)):
        for i, name in enumerate(judges, 1):
            _check_llm_ref(f"{fp}:[{section}].judges[{i}]", name)
        if judges and len(judges) % 2 == 0:
            col.error(f"{fp}:[{section}].judges: 非空时长度须为奇数，得到 {len(judges)} 个")

    if modality == "ui":
        vision_users: dict[str, set[str]] = {}
        if quality.enabled:
            quality_refs = (quality.judges
                            if quality.judges and quality.mode == "pairwise"
                            else (quality.llm,))
            for name in quality_refs:
                vision_users.setdefault(name, set()).add("quality")
        if annotate.enabled:
            vision_users.setdefault(annotate.llm, set()).add("annotate")
        if verify.enabled:
            for name in (verify.judges or (verify.llm,)):
                vision_users.setdefault(name, set()).add("verify")
        for name, stages in vision_users.items():
            prof = llm_profiles.get(name)
            if prof is not None and not prof.supports_vision:
                col.error(f"{fc}:[llm.{name}].supports_vision: UI 模态被 "
                          f"{'/'.join(sorted(stages))} 阶段引用的 profile 须 "
                          f"supports_vision = true，得到 false")

    if dedup.semantic:
        if dedup.semantic_embedding is None:
            col.error(f"{fp}:[dedup].semantic_embedding: dedup.semantic = true 时必填，"
                      f"期望 config.toml [embedding.*] profile 名")
        elif config_ok and dedup.semantic_embedding not in embedding_profiles:
            avail = "、".join(embedding_profiles) if embedding_profiles else "（无）"
            col.error(f"{fp}:[dedup].semantic_embedding: 引用的 profile "
                      f"{_fmt(dedup.semantic_embedding)} 不存在于 config.toml "
                      f"[embedding.*]，可用：{avail}")

    # ── rules 6–9 — cross-field constraints (v1.2) ────────────────────────
    if quality.selection == "top_ratio":
        if quality.top_ratio is None and not p["top_ratio_provided"]:
            col.error(f'{fp}:[quality].top_ratio: selection = "top_ratio" 时必填，'
                      f"期望 (0,1] 内的数值")
        if quality.threshold is not None:
            col.error(f'{fp}:[quality].threshold: 与 quality.top_ratio 互斥'
                      f'（selection = "top_ratio" 时不得设置）')
    elif quality.top_ratio is not None or p["top_ratio_provided"]:
        # Silent-footgun guard (E2E finding P3-7): top_ratio set while the
        # selection stays "threshold" is legal but a no-op — say so loudly.
        col.warn(f'{fp}:[quality].top_ratio: selection 仍为默认 "threshold"，'
                 f'该键不会生效——要按比例定量保留请同时设 selection = "top_ratio"')

    if quality.enabled and quality.judges and quality.mode == "pointwise":
        # Same no-op family: the judges panel is defined over pairwise
        # comparisons only (spec 3.4.4) — pointwise always uses quality.llm.
        col.warn(f'{fp}:[quality].judges: pointwise 模式下评审团不生效'
                 f'（逐条打分恒用 quality.llm）——要用评审团请切 mode = "pairwise"')

    sc = annotate.self_consistency
    if sc != 0 and (sc < 3 or sc % 2 == 0):
        col.error(f"{fp}:[annotate].self_consistency: 期望 0 或 ≥3 的奇数，得到 {sc}")

    if generate.mixture == "weighted":
        if not generate.weights:
            col.error(f'{fp}:[generate].weights: mixture = "weighted" 时必填，'
                      f"期望正数数组（长度 = generate.llms）")
        else:
            if len(generate.weights) != len(generate.llms):
                col.error(f"{fp}:[generate].weights: 期望长度 {len(generate.llms)}"
                          f"（= generate.llms），得到长度 {len(generate.weights)}")
            for i, w in enumerate(generate.weights, 1):
                if not w > 0:
                    col.error(f"{fp}:[generate].weights[{i}]: 期望正数，得到 {_fmt(w)}")
    # style name uniqueness / prompt non-emptiness enforced during parsing

    # ── rules 10/11 — run mode (v1.4; = stage constraint ④) ───────────────
    seed_examples_set = gen_provided["seed_examples"]
    standalone_set = gen_provided["standalone_count"]
    if mode == "generate_only":
        if run["input"] is not None:
            col.error(f'{fp}:[run].input: run.mode = "generate_only" 时必须缺省，'
                      f"得到 {_fmt(run['input'])}")
        if cli.input is not None:
            col.error(f'cli:--input: run.mode = "generate_only" 时不得提供输入路径，'
                      f"得到 {_fmt(cli.input)}")
        if modality != "text":
            col.error(f'{fp}:[run].modality: run.mode = "generate_only" 要求 "text"，'
                      f"得到 {_fmt(modality)}")
        if not generate.enabled:
            col.error(f'{fp}:[generate].enabled: run.mode = "generate_only" 要求 '
                      f"generate.enabled = true")
        if seed_examples_set and standalone_set:
            col.error(f"{fp}:[generate].seed_examples: 与 standalone_count 互斥，"
                      f"恰好提供其一")
        elif not seed_examples_set and not standalone_set:
            col.error(f"{fp}:[generate].seed_examples: generate_only 模式要求提供 "
                      f"seed_examples（非空字符串数组）或 standalone_count（≥ 1）其一")
        elif seed_examples_set:
            if not generate.seed_examples:
                col.error(f"{fp}:[generate].seed_examples: 期望非空字符串数组，得到空数组")
            for i, s in enumerate(generate.seed_examples, 1):
                if not s.strip():
                    col.error(f"{fp}:[generate].seed_examples[{i}]: 期望非空字符串，"
                              f"得到 {_fmt(s)}")
        # standalone_count >= 1 already enforced at parse time
    else:  # process mode
        if seed_examples_set:
            col.error(f'{fp}:[generate].seed_examples: 仅 run.mode = "generate_only" '
                      f"可设置（process 模式不得设置）")
        if standalone_set:
            col.error(f'{fp}:[generate].standalone_count: 仅 run.mode = "generate_only" '
                      f"可设置（process 模式不得设置）")

    # ── rule 12 — API keys for referenced profiles only ───────────────────
    # Quality's judges panel only replaces quality.llm in PAIRWISE mode
    # (spec 3.4.4: pointwise scoring always uses quality.llm; see also
    # cli.referenced_profiles) — the reference sets must agree with runtime.
    quality_judges_active = bool(quality.judges) and quality.mode == "pairwise"
    referenced: set[str] = set()
    if quality.enabled:
        referenced |= set(quality.judges) if quality_judges_active else {quality.llm}
    if annotate.enabled:
        referenced.add(annotate.llm)
    if generate.enabled:
        referenced |= set(generate.llms)
    if verify.enabled:
        referenced |= set(verify.judges) if verify.judges else {verify.llm}
    if output.repair_llm is not None:
        referenced.add(output.repair_llm)

    for name in sorted(referenced):
        prof = llm_profiles.get(name)
        if prof is None or not prof.api_key_env:
            continue  # missing profile / missing api_key_env already reported
        key = os.environ.get(prof.api_key_env, "")
        if not key:
            col.error(f"{fc}:[llm.{name}].api_key_env: 环境变量 "
                      f"{_fmt(prof.api_key_env)} 未设置或为空")
        else:
            llm_profiles[name] = replace(prof, api_key=key)

    if dedup.semantic and dedup.semantic_embedding in embedding_profiles:
        prof_e = embedding_profiles[dedup.semantic_embedding]
        if prof_e.api_key_env:
            key = os.environ.get(prof_e.api_key_env, "")
            if not key:
                col.error(f"{fc}:[embedding.{prof_e.name}].api_key_env: 环境变量 "
                          f"{_fmt(prof_e.api_key_env)} 未设置或为空")
            else:
                embedding_profiles[prof_e.name] = replace(prof_e, api_key=key)

    # ── rules 13–15 — user schema + few-shot examples ─────────────────────
    user_schema, schema_ok = _load_user_schema(col, fp, output)
    if schema_ok and annotate.examples:
        skey = "schema_inline" if output.schema_inline is not None else "schema_path"
        validator = Draft202012Validator(user_schema)
        for i, ex in enumerate(annotate.examples, 1):
            try:
                errs = sorted(validator.iter_errors(ex.output),
                              key=lambda e: list(e.absolute_path))
            except Exception as e:
                # Backstop for resolution failures the rule-13 walk cannot see
                # (e.g. $dynamicRef): iter_errors raises a referencing error
                # (jsonschema.exceptions._WrappedReferencingError /
                # referencing.exceptions.Unresolvable). Per spec 3.1.5 this must
                # join the aggregated ConfigError (exit 2), never escape as an
                # unhandled crash (exit 4). One error suffices — the cause is
                # the schema itself, not any individual example.
                col.error(f"{fp}:[output].{skey}: 用户 Schema 引用无法解析，"
                          f"无法校验 [[annotate.examples]] 示例输出：{e}")
                break
            if errs:
                e0 = errs[0]
                ptr = "/" + "/".join(str(x) for x in e0.absolute_path)
                col.error(f"{fp}:[[annotate.examples]][{i}].output: 未通过用户 Schema："
                          f"{ptr}: {e0.message}")

    # ── rule 17 — validation hooks (v1.5 plan A, spec 3.8.2/3.6.2) ────────
    output_hook = None
    if output.validator is not None:
        try:
            output_hook = resolve_hook(output.validator)
        except ValueError as e:
            col.error(f"{fp}:[output].validator: {e}")
    if generate.enabled and generate.sample_validator is not None:
        try:
            resolve_hook(generate.sample_validator)
        except ValueError as e:
            col.error(f"{fp}:[generate].sample_validator: {e}")
    if output_hook is not None and schema_ok and annotate.examples:
        # Dry-run every few-shot output through the hook: an example the
        # user's own validator rejects is a config error, caught at startup.
        for i, ex in enumerate(annotate.examples, 1):
            try:
                violations = normalize_violations(output_hook(dict(ex.output), None),
                                                  output.validator)
            except Exception as e:  # hook bug — surface as config error, not exit 4
                col.error(f"{fp}:[output].validator: few-shot 干跑第 {i} 条示例时"
                          f"回调抛出异常：{type(e).__name__}: {e}")
                break
            if violations:
                col.error(f"{fp}:[[annotate.examples]][{i}].output: 未通过 "
                          f"output.validator 回调：{violations[0]}")

    # ── rule 16 — rubric resolution + validation ──────────────────────────
    selector = quality.rubric or ("default:ui" if modality == "ui" else "default:text")
    rubric: Rubric
    rubric_is_inline = False
    if selector == "inline":
        if rubric_raw is None:
            col.error(f'{fp}:[quality].rubric: rubric = "inline" 但未提供 [[rubric.criteria]]')
            rubric = _fallback_default_rubric(col, modality)
        else:
            t = _Tbl(col, fp, "[rubric]", rubric_raw)
            name = t.get_str("name", None, required=True, nonempty=True)
            raw_criteria = t.take("criteria")
            t.finish()
            if raw_criteria is _MISSING or (isinstance(raw_criteria, list) and not raw_criteria):
                col.error(f"{fp}:[rubric].criteria: criteria 不得为空，期望非空表数组")
                criteria: tuple[Criterion, ...] = ()
            else:
                criteria = _parse_criteria(col, fp, raw_criteria)
            rubric = Rubric(name=name or "inline", criteria=criteria)
            rubric_is_inline = True
    else:
        try:
            rubric = default_rubric(selector)  # type: ignore[arg-type]
        except Exception as e:  # pragma: no cover — packaged files are shipped valid
            col.error(f"{selector}: 默认 rubric 装载失败：{e}")
            rubric = Rubric(name=selector, criteria=())
        if rubric_raw is not None:
            col.warn(f"{fp}:[[rubric.criteria]]: quality.rubric = {_fmt(selector)}，"
                     f"内联 rubric 未生效，已忽略")

    if quality.mode == "pointwise":
        for i, c in enumerate(rubric.criteria, 1):
            if len(c.pointwise_levels) != 6:
                loc = (f"{fp}:[[rubric.criteria]][{i}].pointwise_levels" if rubric_is_inline
                       else f"{selector}:criteria[{i}].pointwise_levels")
                col.error(f"{loc}: pointwise 模式要求恰好 6 级（0–5），"
                          f"得到 {len(c.pointwise_levels)} 级")

    # ── rules 17–19 — stage combination matrix (spec 2.3.1 ①–③) ───────────
    if not annotate.enabled and not quality.enabled:
        col.error(f"{fp}:[quality].enabled: quality 与 annotate 不得同时禁用"
                  f"（至少启用一个，2.3.1 约束①）")
    if verify.enabled and not annotate.enabled:
        col.error(f"{fp}:[verify].enabled: verify.enabled = true 要求 "
                  f"annotate.enabled = true（2.3.1 约束②）")
    if generate.enabled:
        if modality != "text":
            col.error(f'{fp}:[generate].enabled: generate.enabled = true 要求 '
                      f'run.modality = "text"，得到 {_fmt(modality)}（2.3.1 约束③）')
        if mode == "process" and not quality.enabled:
            col.error(f"{fp}:[generate].enabled: process 模式下 generate.enabled = true "
                      f"要求 quality.enabled = true（种子来自质量门，2.3.1 约束③）")
    # constraint ④ is the generate_only block above (rule 10)

    # ── required-when-enabled instructions (spec §5.2 †) ──────────────────
    if annotate.enabled and not annotate.instruction.strip():
        col.error(f"{fp}:[annotate].instruction: annotate.enabled = true 时必填，"
                  f"期望非空字符串")
    if generate.enabled and not generate.instruction.strip():
        col.error(f"{fp}:[generate].instruction: generate.enabled = true 时必填，"
                  f"期望非空字符串")

    # ── rule 21 — paths ────────────────────────────────────────────────────
    eff_input = cli.input if cli.input is not None else run["input"]
    eff_output = cli.output if cli.output is not None else run["output"]

    if eff_output is None:
        col.error(f"{fp}:[run].output: 缺失必填键，期望字符串（可用 CLI --output 提供）")

    input_path = Path(eff_input) if eff_input else None
    if mode == "process":
        if eff_input is None:
            col.error(f"{fp}:[run].input: process 模式必填（可用 CLI --input 提供）")
        elif eff_output is not None:
            # NOTE: input EXISTENCE/readability is deliberately NOT validated here.
            # Per spec §2.4 (missing path → exit 3, process mode) and the frozen
            # InputError contract ("path missing at run start"), that check belongs
            # to M2 Ingestor.scan()/records(), which raises InputError → exit 3.
            # M1 only checks the output/input path relationship (best-effort when
            # the input does not exist: is_dir()/is_file() are then both False).
            out_res = Path(eff_output).resolve()
            in_res = input_path.resolve()
            if input_path.is_dir() and out_res.is_relative_to(in_res):
                col.error(f"{fp}:[run].output: 不得位于输入目录内部（防止自吞），"
                          f"得到 {_fmt(eff_output)}")
            elif input_path.is_file() and out_res == in_res:
                col.error(f"{fp}:[run].output: 不得与输入文件相同，得到 {_fmt(eff_output)}")

    if eff_output is not None:
        parent = Path(eff_output).resolve().parent
        if not (parent.is_dir() and os.access(parent, os.W_OK)):
            col.error(f"{fp}:[run].output: 输出父目录不存在或不可写，得到 {_fmt(eff_output)}")

    # ── non-blocking warning: self-enhancement bias (spec 3.7.2) ──────────
    if verify.enabled and annotate.enabled:
        a_prof = llm_profiles.get(annotate.llm)
        v_prof = llm_profiles.get(verify.llm) if not verify.judges else None
        if a_prof is not None and v_prof is not None and a_prof.model == v_prof.model:
            col.warn(f"{fp}:[verify].llm: verify.llm 与 annotate.llm 使用同一模型 "
                     f"{_fmt(a_prof.model)}，存在自增强偏差风险（3.7.2）")

    _flush_warnings(col)
    if col.errors:
        raise ConfigError(col.errors)

    # ── assemble the frozen ResolvedConfig ────────────────────────────────
    trace_path = trace.path
    if not trace_path and eff_output:
        trace_path = str(Path(eff_output).with_suffix("")) + ".trace.jsonl"

    return ResolvedConfig(
        tool=ToolConfig(
            log_level=cli.log_level if cli.log_level is not None else tool.log_level,
            log_format=tool.log_format,
        ),
        llm_profiles=llm_profiles,
        embedding_profiles=embedding_profiles,
        run=RunConfig(
            output=eff_output,
            modality=modality,          # type: ignore[arg-type]
            input=None if mode == "generate_only" else eff_input,
            mode=mode,                  # type: ignore[arg-type]
            batch_size=run["batch_size"],
            seed=run["seed"],
            fatal_error_threshold=run["fatal_error_threshold"],
        ),
        input=input_cfg,
        dedup=dedup,
        quality=replace(quality, rubric=selector),
        generate=generate,
        annotate=annotate,
        verify=verify,
        output=output,
        trace=replace(trace, path=trace_path),
        rubric=rubric,
        user_schema=user_schema,
        limit=cli.limit,
        strict=cli.strict,
        dry_run=cli.dry_run,
        config_path=fc,
        project_path=fp,
        config_digest="sha256:" + hashlib.sha256(config_raw or b"").hexdigest(),
        project_digest="sha256:" + hashlib.sha256(project_raw or b"").hexdigest(),
    )


def _fallback_default_rubric(col: _Collector, modality: str) -> Rubric:
    try:
        return default_rubric("default:ui" if modality == "ui" else "default:text")
    except Exception:  # pragma: no cover
        return Rubric(name="inline", criteria=())


def _flush_warnings(col: _Collector) -> None:
    """Unknown keys and advisory findings go to stderr as warnings — never errors
    (spec 3.1.4 TOML-structure row; M12 logging is not configured yet at load time)."""
    for w in col.warnings:
        print(f"warning: {w}", file=sys.stderr)

"""M1 config loader (spec 3.1, CONTRACTS.md §6.2/§6.3).

load(): three-source merge — CLI overrides > project.toml > config.toml/built-in
defaults — plus FULL startup validation. Every validation error is aggregated into
a single ConfigError (never first-error-only); unknown keys produce stderr
warnings only (forward compatibility) — EXCEPT inside the v1.7 [class.*] override
namespace, which M1 explicitly owns: keys outside the whitelist are errors (R25).

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
    ClassifyConfig,
    ClassSpec,
    ClassView,
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

_TRACE_CHANNELS = ("ingest", "dedup", "classify", "quality", "annotate", "verify",
                   "schema", "llm")

# v1.7 [class.<name>.<section>] override whitelist (spec 5.2 / R25): sections and
# keys OUTSIDE this table are CONFIG_ERRORs, not forward-compat warnings — the
# [class.*] namespace is explicitly owned by M1. "rubric" is the per-class inline
# rubric sub-table companion of quality.rubric = "inline" (R7).
_CLASS_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "quality": ("mode", "rounds", "rubric", "threshold", "selection", "top_ratio"),
    "annotate": ("instruction", "examples"),
    "generate": ("instruction", "styles", "num_per_record", "temperature"),
    "verify": ("extra_criteria",),
}
_CLASS_SECTIONS = ("quality", "rubric", "annotate", "generate", "verify")

# The quality selection group (R6): the class providing ANY of these keys takes
# over the whole group — the global side's values are dropped (back to built-in
# defaults) before the class overrides apply, so a global threshold and a class
# top_ratio (or vice versa) never spuriously coexist in the merged view.
_SELECTION_GROUP = ("selection", "threshold", "top_ratio")


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


def _parse_key_envs(col: _Collector, t: _Tbl, data: dict) -> tuple[str, ...]:
    """v1.6 key pool (spec 3.1.4 API-Key row / 5.1): exactly one of
    ``api_key_env`` / ``api_key_envs`` is provided; both forms normalize to a
    non-empty tuple of distinct, non-empty env-var names (scalar → 1-tuple).
    Returns () when the declaration is invalid (errors already collected)."""
    has_single = "api_key_env" in data
    has_multi = "api_key_envs" in data
    # Always consume both keys so finish() never flags them as unknown.
    single = t.get_str("api_key_env", None, nonempty=True)
    multi = t.get_str_tuple("api_key_envs", ())
    if has_single and has_multi:
        col.error(f"{t.loc('api_key_envs')}: 与 api_key_env 互斥（恰提供其一，v1.6）")
        return ()
    if not has_single and not has_multi:
        col.error(f"{t.loc('api_key_env')}: 缺失必填键——api_key_env 与 api_key_envs "
                  f"须恰提供其一（v1.6）")
        return ()
    if has_single:
        return (single,) if single else ()
    if not multi:
        raw = data.get("api_key_envs")
        if isinstance(raw, list) and not raw:
            col.error(f"{t.loc('api_key_envs')}: 期望非空的环境变量名数组（≥1 项）")
        # non-list / bad-element cases: get_str_tuple already collected the
        # per-element errors — no second, misleading error line (review fix).
        return ()
    ok = True
    seen: set[str] = set()
    for i, env in enumerate(multi, 1):
        if not env.strip():
            col.error(f"{t.loc('api_key_envs')}[{i}]: 期望非空字符串，得到 {_fmt(env)}")
            ok = False
        elif env in seen:
            col.error(f"{t.loc('api_key_envs')}[{i}]: 环境变量名 {_fmt(env)} 重复"
                      f"（池内名称须互异）")
            ok = False
        seen.add(env)
    return multi if ok else ()


def _parse_llm_profile(col: _Collector, file: str, name: str, data: dict) -> LLMProfile:
    t = _Tbl(col, file, f"[llm.{name}]", data)
    key_envs = _parse_key_envs(col, t, data)
    prof = LLMProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", required=True,
                           enum=("openai_compatible", "anthropic")),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=key_envs[0] if key_envs else "",
        api_key_envs=key_envs,
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
    key_envs = _parse_key_envs(col, t, data)
    prof = EmbeddingProfile(
        name=name,
        provider=t.get_str("provider", "openai_compatible", enum=("openai_compatible",)),
        base_url=t.get_str("base_url", "", required=True, nonempty=True) or "",
        model=t.get_str("model", "", required=True, nonempty=True) or "",
        api_key_env=key_envs[0] if key_envs else "",
        api_key_envs=key_envs,
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


def _parse_styles(col: _Collector, file: str, raw: Any,
                  section: str = "generate") -> tuple[GenerateStyle, ...]:
    """`section` shifts error locations for the v1.7 per-class styles override
    ("class.<name>.generate"); the default keeps the global [generate] wording."""
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[{section}].styles: 期望表数组，得到 {_fmt(raw)}")
        return ()
    styles: list[GenerateStyle] = []
    seen: set[str] = set()
    for i, sub in enumerate(raw, 1):
        label = f"[[{section}.styles]][{i}]"
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


def _parse_examples(col: _Collector, file: str, raw: Any,
                    section: str = "annotate") -> tuple[FewShotExample, ...]:
    """`section` shifts error locations for the v1.7 per-class examples override
    ("class.<name>.annotate"); the default keeps the global [annotate] wording."""
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[{section}].examples: 期望表数组，得到 {_fmt(raw)}")
        return ()
    examples: list[FewShotExample] = []
    for i, sub in enumerate(raw, 1):
        label = f"[[{section}.examples]][{i}]"
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


def _parse_classes(col: _Collector, file: str, raw: Any) -> tuple[ClassSpec, ...]:
    """Parse the [[classify.classes]] array of tables (spec 5.2 v1.7): name
    matches [a-z0-9_]+ and is unique within the table, description is non-empty,
    examples is an optional string array (input-side few-shot lines only)."""
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        col.error(f"{file}:[classify].classes: 期望表数组，得到 {_fmt(raw)}")
        return ()
    classes: list[ClassSpec] = []
    seen: set[str] = set()
    for i, sub in enumerate(raw, 1):
        label = f"[[classify.classes]][{i}]"
        if not isinstance(sub, dict):
            col.error(f"{file}:{label}: 期望表（table），得到 {_fmt(sub)}")
            continue
        t = _Tbl(col, file, label, sub)
        name = t.get_str("name", None, required=True, nonempty=True)
        if name is not None and not _KEY_RE.fullmatch(name):
            col.error(f"{file}:{label}.name: 期望匹配 [a-z0-9_]+，得到 {_fmt(name)}")
            name = None
        description = t.get_str("description", None, required=True, nonempty=True)
        examples = t.get_str_tuple("examples", ())
        t.finish()
        if name is not None:
            if name in seen:
                col.error(f"{file}:{label}.name: 表内 name 须唯一，得到重复的 {_fmt(name)}")
            seen.add(name)
        if name is not None and description is not None:
            classes.append(ClassSpec(name=name, description=description,
                                     examples=examples))
    return tuple(classes)


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
        max_park_s=t.get_int("max_park_s", 3600, minimum=0),
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

    classify_section = _section(col, top, "classify")
    t = _Tbl(col, file, "[classify]", classify_section)
    classify = ClassifyConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        assignment=t.get_str("assignment", "single", enum=("single", "multi")),
        max_labels=t.get_int("max_labels", None),      # range [2, len(classes)] checked in load()
        instruction=t.get_str("instruction", "") or "",
        fallback_class=t.get_str("fallback_class", "") or "",
        self_consistency=t.get_int("self_consistency", 0, minimum=0),
        sc_temperature=t.get_float("sc_temperature", 0.7, ge=0),
        on_error=t.get_str("on_error", "fallback", enum=("fallback", "fail")),
        classes=_parse_classes(col, file, t.take("classes")),
    )
    # distinguish "explicitly set" from "dataclass default" (same pattern as
    # gen_provided below): max_labels is multi-only, classes drives R8
    classify_provided = {
        "classes": isinstance(classify_section, dict) and "classes" in classify_section,
        "max_labels": isinstance(classify_section, dict) and "max_labels" in classify_section,
    }
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

    # [class.<name>.<section>] (v1.7) is likewise passed through raw: the
    # whitelist check and per-class merge need the resolved global sections
    # AND the resolved global rubric, so load() owns them.
    class_raw = _section(col, top, "class")

    top.finish()
    return dict(
        run=run, input=input_cfg, dedup=dedup, classify=classify,
        classify_provided=classify_provided, class_raw=class_raw,
        quality=quality, generate=generate,
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


# ── rubric resolution / few-shot dry-run / per-class merge (v1.7 helpers) ──


def _resolve_rubric(col: _Collector, file: str, selector: str, raw: Any,
                    modality: str, scope: str = "") -> tuple[Rubric, bool]:
    """Resolve one effective rubric from its (already-defaulted, non-empty)
    selector plus the optional inline table `raw` (None when absent) — the
    load()-tail inline-rubric logic factored out so per-class views can
    re-resolve with merged selectors (R7). `scope` is "" for the global rubric
    or "class.<name>" for a class view; it only shifts error/warning locations
    ([rubric] ↔ [class.<name>.rubric]). Returns (rubric, is_inline)."""
    prefix = f"{scope}." if scope else ""
    if selector == "inline":
        if raw is None:
            col.error(f'{file}:[{prefix}quality].rubric: rubric = "inline" '
                      f'但未提供 [[{prefix}rubric.criteria]]')
            return _fallback_default_rubric(col, modality), False
        t = _Tbl(col, file, f"[{prefix}rubric]", raw)
        name = t.get_str("name", None, required=True, nonempty=True)
        raw_criteria = t.take("criteria")
        t.finish()
        if raw_criteria is _MISSING or (isinstance(raw_criteria, list) and not raw_criteria):
            col.error(f"{file}:[{prefix}rubric].criteria: criteria 不得为空，期望非空表数组")
            criteria: tuple[Criterion, ...] = ()
        else:
            criteria = _parse_criteria(col, file, raw_criteria,
                                       label=f"{prefix}rubric.criteria")
        return Rubric(name=name or "inline", criteria=criteria), True
    try:
        rubric = default_rubric(selector)  # type: ignore[arg-type]
    except Exception as e:  # pragma: no cover — packaged files are shipped valid
        col.error(f"{selector}: 默认 rubric 装载失败：{e}")
        rubric = Rubric(name=selector, criteria=())
    if raw is not None:
        col.warn(f"{file}:[[{prefix}rubric.criteria]]: quality.rubric = {_fmt(selector)}，"
                 f"内联 rubric 未生效，已忽略")
    return rubric, False


def _check_pointwise_rubric(col: _Collector, file: str, rubric: Rubric, *,
                            is_inline: bool, selector: str, scope: str = "") -> None:
    """Pointwise mode requires exactly 6 levels per criterion (spec 3.1.4 rubric
    row). v1.7 runs this on every distinct (effective mode × effective rubric)
    combination — global and per-class (R7); the caller dedupes rubrics already
    checked so shared tables are flagged once."""
    prefix = f"{scope}." if scope else ""
    for i, c in enumerate(rubric.criteria, 1):
        if len(c.pointwise_levels) != 6:
            loc = (f"{file}:[[{prefix}rubric.criteria]][{i}].pointwise_levels" if is_inline
                   else f"{selector}:criteria[{i}].pointwise_levels")
            col.error(f"{loc}: pointwise 模式要求恰好 6 级（0–5），"
                      f"得到 {len(c.pointwise_levels)} 级")


def _dryrun_fewshot(col: _Collector, file: str, examples: tuple[FewShotExample, ...],
                    elem_label: str, *, validator: Any, schema_key: str,
                    hook: Any, hook_ref: str | None) -> tuple[bool, bool]:
    """Dry-run few-shot example outputs through the user schema (rule 14) and
    the output.validator hook (rule 17) — shared by the global [[annotate.
    examples]] and the v1.7 per-class [[class.<name>.annotate.examples]] sets
    (`elem_label` carries the location). Either part is skipped when its
    `validator` / `hook` argument is None. Returns (schema_alive, hook_alive):
    a False flag tells the caller to stop dry-running FURTHER example sets on
    that layer — the cause (unresolvable schema $ref / hook raising) lies in
    the schema or hook itself, so one error line suffices."""
    schema_alive = True
    if validator is not None:
        for i, ex in enumerate(examples, 1):
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
                col.error(f"{file}:[output].{schema_key}: 用户 Schema 引用无法解析，"
                          f"无法校验 [[{elem_label}]] 示例输出：{e}")
                schema_alive = False
                break
            if errs:
                e0 = errs[0]
                ptr = "/" + "/".join(str(x) for x in e0.absolute_path)
                col.error(f"{file}:[[{elem_label}]][{i}].output: 未通过用户 Schema："
                          f"{ptr}: {e0.message}")
    hook_alive = True
    if hook is not None:
        # Dry-run every few-shot output through the hook: an example the
        # user's own validator rejects is a config error, caught at startup.
        for i, ex in enumerate(examples, 1):
            try:
                violations = normalize_violations(hook(dict(ex.output), None), hook_ref)
            except Exception as e:  # hook bug — surface as config error, not exit 4
                col.error(f"{file}:[output].validator: few-shot 干跑第 {i} 条示例时"
                          f"回调抛出异常：{type(e).__name__}: {e}")
                hook_alive = False
                break
            if violations:
                col.error(f"{file}:[[{elem_label}]][{i}].output: 未通过 "
                          f"output.validator 回调：{violations[0]}")
    return schema_alive, hook_alive


def _merge_class_sections(
        col: _Collector, file: str, cname: str, sections: dict,
        base_quality: QualityConfig, base_annotate: AnnotateConfig,
        base_generate: GenerateConfig, base_verify: VerifyConfig,
) -> tuple[QualityConfig, AnnotateConfig, GenerateConfig, VerifyConfig, dict]:
    """Merge one class's [class.<name>.*] override sections onto the resolved
    global configs (spec 5.2 v1.7). Per-key provenance: a key the class provides
    overrides the global value, everything else is inherited. `base_quality`
    carries the defaulted global rubric selector in its `rubric` field.

    - Whitelist (R25): sections outside _CLASS_SECTIONS and keys outside
      _CLASS_SECTION_KEYS are CONFIG_ERRORs — the [class.*] namespace is owned
      by M1, so the forward-compat unknown-key warning does NOT apply here.
    - Selection group (R6): providing ANY of selection/threshold/top_ratio makes
      the class take over the whole group — the unprovided group keys restart
      from the BUILT-IN defaults (not the global values), so a global threshold
      and a class top_ratio (or vice versa) never spuriously coexist. The
      rule-6 family (required-iff / mutual exclusion / no-op warning) then runs
      on the merged view.
    - The [class.<name>.rubric] table is NOT consumed here: rubric re-resolution
      (R7) needs the merged selector, so it is returned raw via `info`.

    Returns (quality, annotate, generate, verify, info) with info =
    {"rubric_raw", "examples_provided"}."""
    for sect, sub in sections.items():
        if sect not in _CLASS_SECTIONS:
            col.error(f"{file}:[class.{cname}.{sect}]: [class.*] 覆盖节不在白名单内"
                      f"（可用：{'、'.join(_CLASS_SECTIONS)}）")
            continue
        if not isinstance(sub, dict):
            col.error(f"{file}:[class.{cname}.{sect}]: 期望表（table），得到 {_fmt(sub)}")
            continue
        if sect == "rubric":
            continue  # structure validated by _resolve_rubric (same as global [rubric])
        allowed = _CLASS_SECTION_KEYS[sect]
        for k in sub:
            if k not in allowed:
                col.error(f"{file}:[class.{cname}.{sect}].{k}: [class.*.{sect}] "
                          f"不可覆盖该键（白名单：{'、'.join(allowed)}）")

    def _sect(name: str) -> dict:
        sub = sections.get(name)
        return sub if isinstance(sub, dict) else {}

    # ── quality: selection-group takeover (R6), then per-key overrides ────
    q_over = _sect("quality")
    group_taken = any(k in q_over for k in _SELECTION_GROUP)
    base_q = (replace(base_quality, selection="threshold", threshold=None, top_ratio=None)
              if group_taken else base_quality)
    t = _Tbl(col, file, f"[class.{cname}.quality]", q_over)
    quality = replace(
        base_q,
        mode=t.get_str("mode", base_q.mode, enum=("pairwise", "pointwise")),
        rounds=t.get_int("rounds", base_q.rounds, minimum=1),
        rubric=t.get_str("rubric", base_q.rubric,
                         enum=("default:text", "default:ui", "inline")),
        threshold=t.get_float("threshold", base_q.threshold, ge=0, le=1),
        selection=t.get_str("selection", base_q.selection,
                            enum=("threshold", "top_ratio")),
        top_ratio=t.get_float("top_ratio", base_q.top_ratio, gt=0, le=1),
    )
    if group_taken:
        # Rule-6 family on the MERGED view (an untouched group was already
        # validated globally, so re-checking would only duplicate errors).
        if quality.selection == "top_ratio":
            if quality.top_ratio is None and "top_ratio" not in q_over:
                col.error(f'{file}:[class.{cname}.quality].top_ratio: selection = '
                          f'"top_ratio" 时必填，期望 (0,1] 内的数值')
            if quality.threshold is not None:
                col.error(f'{file}:[class.{cname}.quality].threshold: 与 '
                          f'quality.top_ratio 互斥（selection = "top_ratio" 时不得设置）')
        elif "top_ratio" in q_over:
            # Same silent-footgun guard as the global P3-7 warning.
            col.warn(f'{file}:[class.{cname}.quality].top_ratio: selection 仍为默认 '
                     f'"threshold"，该键不会生效——要按比例定量保留请同时设 '
                     f'selection = "top_ratio"')

    # ── annotate ───────────────────────────────────────────────────────────
    a_over = _sect("annotate")
    t = _Tbl(col, file, f"[class.{cname}.annotate]", a_over)
    examples_provided = "examples" in a_over
    annotate = replace(
        base_annotate,
        instruction=t.get_str("instruction", base_annotate.instruction, nonempty=True),
        examples=(_parse_examples(col, file, t.take("examples"),
                                  section=f"class.{cname}.annotate")
                  if examples_provided else base_annotate.examples),
    )

    # ── generate ───────────────────────────────────────────────────────────
    g_over = _sect("generate")
    t = _Tbl(col, file, f"[class.{cname}.generate]", g_over)
    generate = replace(
        base_generate,
        instruction=t.get_str("instruction", base_generate.instruction, nonempty=True),
        styles=(_parse_styles(col, file, t.take("styles"),
                              section=f"class.{cname}.generate")
                if "styles" in g_over else base_generate.styles),
        num_per_record=t.get_int("num_per_record", base_generate.num_per_record,
                                 minimum=1),
        temperature=t.get_float("temperature", base_generate.temperature, ge=0),
    )

    # ── verify ─────────────────────────────────────────────────────────────
    v_over = _sect("verify")
    t = _Tbl(col, file, f"[class.{cname}.verify]", v_over)
    verify = replace(
        base_verify,
        extra_criteria=t.get_str("extra_criteria", base_verify.extra_criteria),
    )

    rubric_raw = sections.get("rubric")
    info = {
        "rubric_raw": rubric_raw if isinstance(rubric_raw, dict) else None,
        "examples_provided": examples_provided,
    }
    return quality, annotate, generate, verify, info


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
    classify: ClassifyConfig = p["classify"]
    classify_provided: dict[str, bool] = p["classify_provided"]
    class_raw: Any = p["class_raw"]
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

    if classify.enabled:
        # like verify below: the default reference ("default") need not exist
        # while the stage is disabled (v1.7, R24 reference-set point ①)
        _check_llm_ref(f"{fp}:[classify].llm", classify.llm)
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
        if classify.enabled:
            vision_users.setdefault(classify.llm, set()).add("classify")
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
    if classify.enabled:
        referenced.add(classify.llm)     # v1.7, R24 reference-set point ②
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

    def _resolve_keys(kind: str, prof_name: str,
                      envs: tuple[str, ...]) -> tuple[str, ...] | None:
        """Resolve EVERY listed env var of a referenced profile (v1.6 pools:
        one aggregated error line per missing variable). Returns the aligned
        key tuple, or None when at least one variable is missing/empty."""
        pooled = len(envs) > 1
        keys: list[str] = []
        ok = True
        for i, env in enumerate(envs, 1):
            key = os.environ.get(env, "")
            if not key:
                loc = (f"{fc}:[{kind}.{prof_name}].api_key_envs[{i}]" if pooled
                       else f"{fc}:[{kind}.{prof_name}].api_key_env")
                col.error(f"{loc}: 环境变量 {_fmt(env)} 未设置或为空")
                ok = False
            keys.append(key)
        return tuple(keys) if ok else None

    for name in sorted(referenced):
        prof = llm_profiles.get(name)
        if prof is None or not prof.api_key_envs:
            continue  # missing profile / invalid key declaration already reported
        keys = _resolve_keys("llm", name, prof.api_key_envs)
        if keys is not None:
            llm_profiles[name] = replace(prof, api_key=keys[0], api_keys=keys)

    if dedup.semantic and dedup.semantic_embedding in embedding_profiles:
        prof_e = embedding_profiles[dedup.semantic_embedding]
        if prof_e.api_key_envs:
            keys = _resolve_keys("embedding", prof_e.name, prof_e.api_key_envs)
            if keys is not None:
                embedding_profiles[prof_e.name] = replace(
                    prof_e, api_key=keys[0], api_keys=keys)

    # ── rules 13–15 — user schema + few-shot examples ─────────────────────
    user_schema, schema_ok = _load_user_schema(col, fp, output)
    skey = "schema_inline" if output.schema_inline is not None else "schema_path"
    schema_validator = Draft202012Validator(user_schema) if schema_ok else None
    schema_alive = True                  # False once a $ref-resolution backstop fired
    if schema_validator is not None and annotate.examples:
        schema_alive, _ = _dryrun_fewshot(
            col, fp, annotate.examples, "annotate.examples",
            validator=schema_validator, schema_key=skey, hook=None, hook_ref=None)

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
    hook_alive = True                    # False once the hook itself raised
    if output_hook is not None and schema_ok and annotate.examples:
        _, hook_alive = _dryrun_fewshot(
            col, fp, annotate.examples, "annotate.examples",
            validator=None, schema_key=skey, hook=output_hook,
            hook_ref=output.validator)

    # ── rule 16 — rubric resolution + validation ──────────────────────────
    selector = quality.rubric or ("default:ui" if modality == "ui" else "default:text")
    rubric, rubric_is_inline = _resolve_rubric(col, fp, selector, rubric_raw, modality)
    if quality.mode == "pointwise":
        _check_pointwise_rubric(col, fp, rubric, is_inline=rubric_is_inline,
                                selector=selector)

    # ── v1.7 — classify + per-class views (spec 5.2; R6/R7/R8/R24/R25) ────
    sc_c = classify.self_consistency
    if sc_c != 0 and (sc_c < 3 or sc_c % 2 == 0):
        col.error(f"{fp}:[classify].self_consistency: 期望 0 或 ≥3 的奇数，得到 {sc_c}")
    if classify_provided["max_labels"] and classify.assignment != "multi":
        col.error(f'{fp}:[classify].max_labels: 仅 assignment = "multi" 时可设置')

    class_views: dict[str, ClassView] = {}
    class_names = tuple(c.name for c in classify.classes)
    if not classify.enabled:
        # R8: parked class config is legal — warn once, naming the ignored
        # tables (aligned with the top_ratio no-op family, NOT an error).
        ignored = (["[[classify.classes]]"] if classify_provided["classes"] else [])
        if isinstance(class_raw, dict):
            ignored += [f"[class.{n}]" for n in class_raw]
        if ignored:
            col.warn(f"{fp}:[classify].enabled: classify.enabled = false，"
                     f"{'、'.join(ignored)} 不会生效，已忽略（留配置、关开关合法）")
    else:
        avail = "、".join(class_names) if class_names else "（无）"
        if len(classify.classes) < 2:
            col.error(f"{fp}:[classify].classes: classify.enabled = true 时须声明 "
                      f"≥ 2 个类别（[[classify.classes]] 表数组），"
                      f"得到 {len(classify.classes)} 个")
        if not classify.fallback_class:
            col.error(f"{fp}:[classify].fallback_class: classify.enabled = true 时必填，"
                      f"期望 [[classify.classes]] 中的类名")
        elif class_names and classify.fallback_class not in class_names:
            col.error(f"{fp}:[classify].fallback_class: 引用的类名 "
                      f"{_fmt(classify.fallback_class)} 不在 [[classify.classes]] 中，"
                      f"可用：{avail}")
        if (classify.max_labels is not None and len(class_names) >= 2
                and not 2 <= classify.max_labels <= len(class_names)):
            col.error(f"{fp}:[classify].max_labels: 期望 [2, {len(class_names)}] "
                      f"内的整数（上界 = 类别数），得到 {classify.max_labels}")
        if classify.max_labels is None:
            classify = replace(classify, max_labels=len(class_names))  # spec 5.2 backfill

        if isinstance(class_raw, dict):
            for cname in class_raw:
                if cname not in class_names:
                    col.error(f"{fp}:[class.{cname}]: 类名 {_fmt(cname)} 不在 "
                              f"[[classify.classes]] 中，可用：{avail}")

        # Materialize one merged view PER DECLARED CLASS (zero-override classes
        # included) so downstream operators never fall back at runtime.
        base_q = replace(quality, rubric=selector)
        global_rubric_key = "[[rubric.criteria]]" if rubric_is_inline else selector
        pointwise_checked: set[str] = (
            {global_rubric_key} if quality.mode == "pointwise" else set())
        for cspec in classify.classes:
            cname = cspec.name
            sections = class_raw.get(cname) if isinstance(class_raw, dict) else None
            if sections is not None and not isinstance(sections, dict):
                col.error(f"{fp}:[class.{cname}]: 期望表（table），得到 {_fmt(sections)}")
                sections = None
            if sections:
                q_c, a_c, g_c, v_c, info = _merge_class_sections(
                    col, fp, cname, sections, base_q, annotate, generate, verify)
            else:
                q_c, a_c, g_c, v_c = base_q, annotate, generate, verify
                info = {"rubric_raw": None, "examples_provided": False}

            # rubric (R7): merged selector → re-resolve; per-key provenance for
            # the inline table ([class.<name>.rubric] beats the global [rubric])
            raw_c = info["rubric_raw"]
            if q_c.rubric == "inline":
                if raw_c is not None:
                    rubric_c, inline_c = _resolve_rubric(
                        col, fp, "inline", raw_c, modality, scope=f"class.{cname}")
                    rkey, rscope = f"[[class.{cname}.rubric.criteria]]", f"class.{cname}"
                elif selector == "inline":
                    # inherited global inline product (incl. its fallback path)
                    rubric_c, inline_c = rubric, rubric_is_inline
                    rkey, rscope = global_rubric_key, ""
                else:
                    # class switched to inline without providing its table —
                    # same rule as global: inline requires the companion table
                    col.error(f'{fp}:[class.{cname}.quality].rubric: rubric = '
                              f'"inline" 但未提供 [[class.{cname}.rubric.criteria]]')
                    rubric_c = _fallback_default_rubric(col, modality)
                    inline_c, rkey, rscope = False, None, ""
            else:
                if raw_c is not None:
                    col.warn(f"{fp}:[[class.{cname}.rubric.criteria]]: quality.rubric = "
                             f"{_fmt(q_c.rubric)}，内联 rubric 未生效，已忽略")
                if q_c.rubric == selector and not rubric_is_inline:
                    rubric_c = rubric    # same packaged default as the global one
                else:
                    try:
                        rubric_c = default_rubric(q_c.rubric)  # type: ignore[arg-type]
                    except Exception as e:  # pragma: no cover — shipped valid
                        col.error(f"{q_c.rubric}: 默认 rubric 装载失败：{e}")
                        rubric_c = Rubric(name=q_c.rubric, criteria=())
                inline_c, rkey, rscope = False, q_c.rubric, ""

            # pointwise 6-level check on the (class mode × class rubric)
            # combination; rubrics already checked are skipped (dedup).
            if q_c.mode == "pointwise" and rkey is not None and rkey not in pointwise_checked:
                pointwise_checked.add(rkey)
                _check_pointwise_rubric(col, fp, rubric_c, is_inline=inline_c,
                                        selector=rkey, scope=rscope)

            # class-provided examples dry-run against the GLOBAL user schema and
            # validator hook (inherited examples were already dry-run above)
            if info["examples_provided"] and a_c.examples:
                v_arg = schema_validator if schema_alive else None
                h_arg = output_hook if (hook_alive and schema_ok) else None
                s_ok, h_ok = _dryrun_fewshot(
                    col, fp, a_c.examples, f"class.{cname}.annotate.examples",
                    validator=v_arg, schema_key=skey, hook=h_arg,
                    hook_ref=output.validator)
                schema_alive = schema_alive and s_ok
                hook_alive = hook_alive and h_ok

            class_views[cname] = ClassView(name=cname, quality=q_c, rubric=rubric_c,
                                           annotate=a_c, generate=g_c, verify=v_c)

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
            max_park_s=run["max_park_s"],
        ),
        input=input_cfg,
        dedup=dedup,
        classify=classify,               # max_labels already backfilled when enabled
        quality=replace(quality, rubric=selector),
        generate=generate,
        annotate=annotate,
        verify=verify,
        output=output,
        trace=replace(trace, path=trace_path),
        rubric=rubric,
        class_views=class_views,
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

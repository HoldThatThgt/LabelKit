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
import importlib.util
import json
import os
import re
import sys
import tomllib
from dataclasses import replace
from importlib import resources
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urljoin

from jsonschema.exceptions import SchemaError
from jsonschema.validators import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from labelkit.common.config.model import (
    AnnotateConfig,
    ClassifyConfig,
    ClassSpec,
    ClassView,
    CliOverrides,
    ConsoleConfig,
    Criterion,
    DedupConfig,
    EmbeddingProfile,
    ExtractConfig,
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
    SegmentConfig,
    StitchConfig,
    StreamConfig,
    ToolConfig,
    TraceConfig,
    VerifyConfig,
)
from labelkit.common.errors import ConfigError
from labelkit.common.extensions.hooks import normalize_violations, resolve_hook
from labelkit.common.runtime import budget

__all__ = ["load", "default_rubric"]

_MISSING = object()

# v1.10 (spec 3.1.4 console row / §7.7): the rich-importability probe is
# find_spec ONLY — the loader never imports rich (lazy import stays a CLI-layer
# concern, U4/U21). Module-level alias so offline tests can inject the probe.
_find_spec = importlib.util.find_spec

_KEY_RE = re.compile(r"[a-z0-9_]+")

_RUBRIC_PKG_FILES: dict[str, str] = {
    "default:text": "default_text.toml",
    "default:ui": "default_ui.toml",
    "default:trajectory": "default_trajectory.toml",   # v1.8 (S29)
}

# v1.9: 11 values — "stitch" joined (T16; channel = stage name, S1: the
# event-name first segment routes automatically, classify precedent).
_TRACE_CHANNELS = ("ingest", "dedup", "segment", "stitch", "extract", "classify",
                   "quality", "annotate", "verify", "schema", "llm")

# rubric selectors accepted at both selector sites (global [quality].rubric and
# per-class [class.<name>.quality].rubric): the packaged defaults + "inline".
_RUBRIC_SELECTORS = ("default:text", "default:ui", "default:trajectory", "inline")

# v1.7 [class.<name>.<section>] override whitelist (spec 5.2 / R25): sections and
# keys OUTSIDE this table are CONFIG_ERRORs, not forward-compat warnings — the
# [class.*] namespace is explicitly owned by M1. "rubric" is the per-class inline
# rubric sub-table companion of quality.rubric = "inline" (R7). v1.8 adds
# "extract" (instruction only, S2); segment stays OUT of the whitelist — it runs
# before classify, so class labels do not exist yet (chain-order causality).
_CLASS_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "quality": ("mode", "rounds", "rubric", "threshold", "selection", "top_ratio"),
    "annotate": ("instruction", "examples"),
    "generate": ("instruction", "styles", "num_per_record", "temperature"),
    "verify": ("extra_criteria",),
    "extract": ("instruction",),
}
_CLASS_SECTIONS = ("quality", "rubric", "annotate", "generate", "verify", "extract")

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


def _parse_console(col: _Collector, file: str, data: Any) -> ConsoleConfig:
    """v1.10 (spec 5.1 [console] / 3.1.4 console row): tool-level three-mode
    console section, whole section optional. mode enum, refresh_hz ∈ [1, 10],
    heartbeat_s ≥ 0 — violations are AGGREGATED CONFIG_ERRORs (never
    first-raise); unknown keys inside [console] stay forward-compat warnings
    (standard finish()). ``mode_resolved`` keeps its dataclass default here —
    load() freezes the real verdict at its end (U21)."""
    t = _Tbl(col, file, "[console]", data)
    mode = t.get_str("mode", "auto", enum=("auto", "rich", "plain"))
    refresh_hz = t.get_int("refresh_hz", 5)
    if not 1 <= refresh_hz <= 10:
        col.error(f"{file}:[console].refresh_hz: 期望 [1, 10] 内的整数"
                  f"（rich 画布重绘频率），得到 {refresh_hz}")
        refresh_hz = 5
    console = ConsoleConfig(
        mode=mode,
        refresh_hz=refresh_hz,
        heartbeat_s=t.get_int("heartbeat_s", 0, minimum=0),
        estimate=t.get_bool("estimate", False),
        interactive=t.get_bool("interactive", True),
    )
    t.finish()
    return console


def _auto_console_mode(*, isatty: bool, log_format: str, term: str | None,
                       rich_importable: bool) -> Literal["rich", "plain"]:
    """v1.10 auto decision chain (spec §7.7 / 3.1.4 console row, U5/U25):
    rich iff stderr.isatty() ∧ tool.log_format == "text" ∧ TERM 非 "dumb"/空
    ∧ rich importable (find_spec probe). NO_COLOR does NOT participate (U25 —
    rich natively strips color while keeping layout); TERM is a terminal
    capability probe, not a config channel (§2.6 no-env-var rule untouched).
    Pure function over injected probe values so every branch is offline-testable."""
    if (isatty and log_format == "text" and term not in ("", "dumb", None)
            and rich_importable):
        return "rich"
    return "plain"


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
        context_window=t.get_int("context_window", 0, minimum=0),
        temperature=t.get_float("temperature", 0.0, ge=0),
        max_image_px=t.get_int("max_image_px", 2048, minimum=1),
        default_image_px=t.get_int("default_image_px", 0, minimum=0),
        price_per_mtok_in=t.get_float("price_per_mtok_in", None, ge=0),
        price_per_mtok_out=t.get_float("price_per_mtok_out", None, ge=0),
    )
    t.finish()
    # v1.11 (V6, spec 3.1.4 上下文预算行): a declared window must leave a
    # positive input budget — cw > max_output_tokens + margin (0 = undeclared
    # = budget off, always legal).
    if prof.context_window > 0 and budget.input_budget(prof) <= 0:
        col.error(f"{file}:[llm.{name}].context_window: 声明窗口下预算非正——须满足 "
                  f"context_window > max_output_tokens + margin"
                  f"（margin = max(256, ceil(0.10 × context_window)) = "
                  f"{budget.margin(prof.context_window)}），得到 context_window = "
                  f"{prof.context_window}, max_output_tokens = {prof.max_output_tokens}")
    # v1.11 (V18): the sampling working point never exceeds the ceiling.
    if prof.default_image_px > 0 and prof.default_image_px > prof.max_image_px:
        col.error(f"{file}:[llm.{name}].default_image_px: 期望 ≤ max_image_px"
                  f"（{prof.max_image_px}），得到 {prof.default_image_px}")
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
        context_window=t.get_int("context_window", 0, minimum=0),
        dims=t.get_int("dims", None, minimum=1),
    )
    t.finish()
    # v1.11 (V15): embed budget = cw − margin (no output reservation) — a
    # declared window must leave it positive.
    if prof.context_window > 0 and budget.embed_budget(prof) <= 0:
        col.error(f"{file}:[embedding.{name}].context_window: 声明窗口下预算非正——"
                  f"须满足 context_window > margin"
                  f"（margin = max(256, ceil(0.10 × context_window)) = "
                  f"{budget.margin(prof.context_window)}），得到 {prof.context_window}")
    return prof


def _parse_config_file(col: _Collector, file: str, data: dict) -> tuple[
        ToolConfig, ConsoleConfig, bool,
        dict[str, LLMProfile], dict[str, EmbeddingProfile]]:
    top = _Tbl(col, file, "", data)
    _check_schema_version(col, top)
    tool = _parse_tool(col, file, _section(col, top, "tool"))
    # v1.10: [console] is an OWNED top-level table now — taken here so finish()
    # below never flags it as an unknown key. The explicit-rich probe reads the
    # RAW table (dataclass default "auto" must not count as intent, U21/§7.7).
    console_section = _section(col, top, "console")
    console = _parse_console(col, file, console_section)
    console_rich_explicit = (isinstance(console_section, dict)
                             and console_section.get("mode") == "rich")

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
    return tool, console, console_rich_explicit, llm_profiles, embedding_profiles


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

    stream_section = _section(col, top, "stream")
    t = _Tbl(col, file, "[stream]", stream_section)
    stream = StreamConfig(
        order_by=t.get_str("order_by", "input_order", nonempty=True) or "input_order",
        on_disorder=t.get_str("on_disorder", "skip", enum=("skip", "fail")),
        key=t.get_str_tuple("key", ()),
        gap_s=t.get_int("gap_s", 300, minimum=0),
        gap_steps=t.get_int("gap_steps", 0, minimum=0),
        session_max_len=t.get_int("session_max_len", 200, minimum=1),
        session_max_span_s=t.get_int("session_max_span_s", 0, minimum=0),
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

    segment_section = _section(col, top, "segment")
    t = _Tbl(col, file, "[segment]", segment_section)
    segment = SegmentConfig(
        enabled=t.get_bool("enabled", False),
        strategy=t.get_str("strategy", "hybrid", enum=("rules", "llm", "hybrid")),
        llm=t.get_str("llm", "default", nonempty=True),
        window=t.get_int("window", 20, minimum=1),   # >= 2 checked in load() (§3.6)
        digest_max_chars=t.get_int("digest_max_chars", 400, minimum=1),
        noise_filter=t.get_bool("noise_filter", True),
        min_len=t.get_int("min_len", 2, minimum=1),
        context=t.get_str("context", "") or "",
        on_error=t.get_str("on_error", "keep", enum=("keep", "fail")),
        # vision_resolved is a parse PRODUCT — frozen at load() end (V1)
    )
    # v1.11 (V2/V27②): `use_vision` was REMOVED — its explicit presence is a
    # DIRECTED CONFIG_ERROR raised in load() via the raw-section probe below,
    # never the unknown-key forward-compat WARN (marking it seen suppresses
    # the WARN so the targeted error is the single report).
    t.seen.add("use_vision")
    t.finish()

    stitch_section = _section(col, top, "stitch")
    t = _Tbl(col, file, "[stitch]", stitch_section)
    stitch = StitchConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        max_open=t.get_int("max_open", 4, minimum=1),
        bias=t.get_str("bias", "conservative", enum=("conservative", "llm")),
        rescue_short=t.get_bool("rescue_short", True),
        repass=t.get_bool("repass", True),
        stale_gap_steps=t.get_int("stale_gap_steps", 0, minimum=0),
        digest_max_chars=t.get_int("digest_max_chars", 400, minimum=1),
        context=t.get_str("context", "") or "",
        votes=t.get_int("votes", 1, minimum=1),   # odd required, checked in load() (T17)
        on_error=t.get_str("on_error", "keep", enum=("keep", "fail")),
    )
    t.finish()

    extract_section = _section(col, top, "extract")
    t = _Tbl(col, file, "[extract]", extract_section)
    extract = ExtractConfig(
        enabled=t.get_bool("enabled", False),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        include_diff=t.get_bool("include_diff", True),
        on_error=t.get_str("on_error", "fallback", enum=("fallback", "fail")),
    )
    t.finish()

    # v1.8 provided-ness probes (classify_provided pattern): section presence
    # drives the no-op warnings, explicit keys drive intent-sensitive checks.
    # "non_switch_keys" = the section carries parked payload beyond its own
    # enabled switch (naming "[segment]" as ignored because of the switch that
    # ignores it would be self-referential).
    stream_provided = {
        "section": stream_section is not None,
        "gap_s": isinstance(stream_section, dict) and "gap_s" in stream_section,
    }
    segment_provided = {
        "non_switch_keys": (isinstance(segment_section, dict)
                            and any(k != "enabled" for k in segment_section)),
        # v1.11 (V2/V27②) raw-section probe: the removed key's presence,
        # independent of the unknown-key path.
        "use_vision": (isinstance(segment_section, dict)
                       and "use_vision" in segment_section),
    }
    stitch_provided = {
        "non_switch_keys": (isinstance(stitch_section, dict)
                            and any(k != "enabled" for k in stitch_section)),
    }
    extract_provided = {
        "non_switch_keys": (isinstance(extract_section, dict)
                            and any(k != "enabled" for k in extract_section)),
    }

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
        rubric=t.get_str("rubric", "", enum=_RUBRIC_SELECTORS) or "",
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

    annotate_section = _section(col, top, "annotate")
    t = _Tbl(col, file, "[annotate]", annotate_section)
    annotate = AnnotateConfig(
        enabled=t.get_bool("enabled", True),
        llm=t.get_str("llm", "default", nonempty=True),
        instruction=t.get_str("instruction", "") or "",
        examples=_parse_examples(col, file, t.take("examples")),
        self_consistency=t.get_int("self_consistency", 0, minimum=0),
        sc_temperature=t.get_float("sc_temperature", 0.7, ge=0),
        sequence_frames=t.get_int("sequence_frames", 20, minimum=1),  # [2,100] in load()
    )
    sequence_frames_provided = (isinstance(annotate_section, dict)
                                and "sequence_frames" in annotate_section)
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
        run=run, input=input_cfg, stream=stream, dedup=dedup,
        segment=segment, stitch=stitch, extract=extract, classify=classify,
        classify_provided=classify_provided, class_raw=class_raw,
        stream_provided=stream_provided, segment_provided=segment_provided,
        stitch_provided=stitch_provided, extract_provided=extract_provided,
        sequence_frames_provided=sequence_frames_provided,
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
        base_extract: ExtractConfig,
) -> tuple[QualityConfig, AnnotateConfig, GenerateConfig, VerifyConfig,
           ExtractConfig, dict]:
    """Merge one class's [class.<name>.*] override sections onto the resolved
    global configs (spec 5.2 v1.7; v1.8 adds the extract section, S2). Per-key
    provenance: a key the class provides overrides the global value, everything
    else is inherited. `base_quality` carries the defaulted global rubric
    selector in its `rubric` field.

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

    Returns (quality, annotate, generate, verify, extract, info) with info =
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
        rubric=t.get_str("rubric", base_q.rubric, enum=_RUBRIC_SELECTORS),
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

    # ── extract (v1.8, S2: instruction is the whole whitelist) ────────────
    e_over = _sect("extract")
    t = _Tbl(col, file, f"[class.{cname}.extract]", e_over)
    extract = replace(
        base_extract,
        instruction=t.get_str("instruction", base_extract.instruction),
    )

    rubric_raw = sections.get("rubric")
    info = {
        "rubric_raw": rubric_raw if isinstance(rubric_raw, dict) else None,
        "examples_provided": examples_provided,
    }
    return quality, annotate, generate, verify, extract, info


# ── public API ─────────────────────────────────────────────────────────────


def default_rubric(
        name: Literal["default:text", "default:ui", "default:trajectory"]) -> Rubric:
    """Load a packaged default rubric from labelkit/data/rubrics/*.toml
    (importlib.resources)."""
    try:
        fname = _RUBRIC_PKG_FILES[name]
    except KeyError:
        raise ValueError(
            f'unknown default rubric {name!r}; expected "default:text", '
            f'"default:ui" or "default:trajectory"'
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
    console = ConsoleConfig()
    console_rich_explicit = False
    llm_profiles: dict[str, LLMProfile] = {}
    embedding_profiles: dict[str, EmbeddingProfile] = {}
    if config_ok:
        (tool, console, console_rich_explicit,
         llm_profiles, embedding_profiles) = _parse_config_file(col, fc, config_data)

    if project_ok:
        p = _parse_project_file(col, fp, project_data)
    else:
        p = None

    if p is None:
        _flush_warnings(col)
        raise ConfigError(col.errors or [f"{fp}: 配置装载失败"])

    run: dict[str, Any] = p["run"]
    input_cfg: InputConfig = p["input"]
    stream: StreamConfig = p["stream"]
    dedup: DedupConfig = p["dedup"]
    segment: SegmentConfig = p["segment"]
    stitch: StitchConfig = p["stitch"]
    extract: ExtractConfig = p["extract"]
    classify: ClassifyConfig = p["classify"]
    classify_provided: dict[str, bool] = p["classify_provided"]
    class_raw: Any = p["class_raw"]
    stream_provided: dict[str, bool] = p["stream_provided"]
    segment_provided: dict[str, bool] = p["segment_provided"]
    stitch_provided: dict[str, bool] = p["stitch_provided"]
    extract_provided: dict[str, bool] = p["extract_provided"]
    sequence_frames_provided: bool = p["sequence_frames_provided"]
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

    if segment.enabled and segment.strategy in ("llm", "hybrid"):
        # v1.8 S30: rules strategy makes zero LLM calls — segment.llm joins the
        # reference sets only when a strategy actually dials out
        _check_llm_ref(f"{fp}:[segment].llm", segment.llm)
    if stitch.enabled:
        # v1.9 T16/T17: pure-text judgment — referenced whenever enabled,
        # never added to the vision-required set below
        _check_llm_ref(f"{fp}:[stitch].llm", stitch.llm)
    if classify.enabled:
        # like verify below: the default reference ("default") need not exist
        # while the stage is disabled (v1.7, R24 reference-set point ①)
        _check_llm_ref(f"{fp}:[classify].llm", classify.llm)
    if extract.enabled:
        _check_llm_ref(f"{fp}:[extract].llm", extract.llm)   # v1.8 S30: always when enabled
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
        # v1.11 (V3): segment is ADAPTIVE about vision (vision_resolved parse
        # product) and never joins the vision-required set — the former
        # use_vision-gated branch lost its failability; "segment" can no
        # longer appear in the stages set of this error.
        vision_users: dict[str, set[str]] = {}
        if classify.enabled:
            vision_users.setdefault(classify.llm, set()).add("classify")
        if extract.enabled:
            # v1.8 S30: extraction reads adjacent screenshots — vision always
            vision_users.setdefault(extract.llm, set()).add("extract")
        if quality.enabled and not segment.enabled:
            # v1.8 S30 relaxation: stream-mode quality scores sequences as pure
            # text (transitions + frame digests, no images)
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
    if segment.enabled and segment.strategy in ("llm", "hybrid"):
        referenced.add(segment.llm)      # v1.8, S30 reference-set point ②
    if stitch.enabled:
        referenced.add(stitch.llm)       # v1.9, T17 reference-set point
    if classify.enabled:
        referenced.add(classify.llm)     # v1.7, R24 reference-set point ②
    if extract.enabled:
        referenced.add(extract.llm)      # v1.8, S30 reference-set point ②
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
    # v1.8 S29: in stream mode the empty selector resolves to the trajectory
    # rubric for BOTH modalities (per-frame default:ui criteria are meaningless
    # for imageless sequence scoring); an explicit selector always wins, and
    # per-class views inherit through the backfilled base selector.
    if quality.rubric:
        selector = quality.rubric
    elif segment.enabled:
        selector = "default:trajectory"
    else:
        selector = "default:ui" if modality == "ui" else "default:text"
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
                q_c, a_c, g_c, v_c, e_c, info = _merge_class_sections(
                    col, fp, cname, sections, base_q, annotate, generate, verify,
                    extract)
            else:
                q_c, a_c, g_c, v_c, e_c = base_q, annotate, generate, verify, extract
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
                                           annotate=a_c, generate=g_c, verify=v_c,
                                           extract=e_c)

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

    # ── v1.8 §3.6 — stream / segment / extract constraints ────────────────
    if segment.enabled:
        if mode != "process":
            col.error(f'{fp}:[segment].enabled: segment.enabled = true 要求 '
                      f'run.mode = "process"，得到 {_fmt(mode)}')
        if generate.enabled:
            col.error(f"{fp}:[segment].enabled: segment.enabled = true 与 "
                      f"generate.enabled = true 互斥（stream 模式不做生成扩增）")
        if not annotate.enabled:
            col.error(f"{fp}:[segment].enabled: segment.enabled = true 要求 "
                      f"annotate.enabled = true（约束⑭：episode 须经标注落用户 Schema）")
    if stitch.enabled and not segment.enabled:
        # v1.9 T17: stitch consumes segment products only
        col.error(f"{fp}:[stitch].enabled: stitch.enabled = true 要求 "
                  f"segment.enabled = true（线索缝合仅作用于分段产物）")
    if stitch.votes % 2 == 0:
        # v1.9 T18/M-4: strict majority needs an odd sample count (judges /
        # classify.self_consistency precedent)
        col.error(f"{fp}:[stitch].votes: 期望 ≥ 1 的奇数（(verdict, thread_ref) "
                  f"严格多数决），得到 {stitch.votes}")
    if extract.enabled:
        if not segment.enabled:
            col.error(f"{fp}:[extract].enabled: extract.enabled = true 要求 "
                      f"segment.enabled = true（转移摘取仅作用于序列记录）")
        if modality != "ui":
            col.error(f'{fp}:[extract].enabled: extract.enabled = true 要求 '
                      f'run.modality = "ui"，得到 {_fmt(modality)}（文本序列 v1 不适用）')

    order_is_meta = stream.order_by.startswith("meta:")
    if stream.order_by != "input_order" and not (order_is_meta
                                                 and stream.order_by[len("meta:"):]):
        col.error(f'{fp}:[stream].order_by: 期望 "input_order" | "meta:<field>"，'
                  f"得到 {_fmt(stream.order_by)}")
    elif order_is_meta and modality != "text":
        col.error(f'{fp}:[stream].order_by: "meta:<field>" 仅文本模态可用'
                  f'（run.modality = "text"），得到 modality {_fmt(modality)}')
    if stream.session_max_span_s > 0 and not order_is_meta:
        # necessarily explicit (default 0 disables) — a hard error
        col.error(f'{fp}:[stream].session_max_span_s: > 0 要求 order_by = '
                  f'"meta:<field>"（时间跨度需时间序键），得到 order_by '
                  f"{_fmt(stream.order_by)}")
    if stream_provided["gap_s"] and not order_is_meta:
        # gap_s carries a non-zero default (300) — only an EXPLICIT value states
        # user intent, and the miss is advisory, not fatal (spec §3.6)
        col.warn(f'{fp}:[stream].gap_s: 显式设置了 gap_s 但 order_by 非 "meta:<field>"，'
                 f'时间差断开不会生效——要按时间断开请设 order_by = "meta:<字段名>"')
    for i, k in enumerate(stream.key, 1):
        if k == "source_dir":
            continue
        if k.startswith("meta:") and k[len("meta:"):]:
            if modality != "text":
                col.error(f'{fp}:[stream].key[{i}]: "meta:<field>" 分区键仅文本模态'
                          f"可用，得到 {_fmt(k)}")
        else:
            col.error(f'{fp}:[stream].key[{i}]: 期望 "meta:<field>"（仅文本）| '
                      f'"source_dir"，得到 {_fmt(k)}')
    if segment.window < 2:
        col.error(f"{fp}:[segment].window: 期望 ≥ 2 的整数（滑窗须含至少一对相邻帧），"
                  f"得到 {segment.window}")
    if not 2 <= annotate.sequence_frames <= 100:
        col.error(f"{fp}:[annotate].sequence_frames: 期望 [2, 100] 内的整数，"
                  f"得到 {annotate.sequence_frames}")

    if segment.enabled:
        if annotate.sequence_frames > 20:
            prof_a = llm_profiles.get(annotate.llm)
            if prof_a is not None and prof_a.max_image_px > 2000:
                # S28: Anthropic hard-rejects >20-image requests carrying any
                # image with an edge > 2000px (400, NOT auto-downscaled)
                col.warn(f"{fp}:[annotate].sequence_frames: sequence_frames = "
                         f"{annotate.sequence_frames} > 20 且被 annotate 引用的 "
                         f"profile [llm.{annotate.llm}] max_image_px = "
                         f"{prof_a.max_image_px} > 2000——Anthropic 对 >20 图请求"
                         f"硬拒任一边 >2000px 的图（400，非自动缩放），请将 "
                         f"max_image_px 改为 ≤ 2000 或降回 sequence_frames ≤ 20")
        if stream.session_max_len > run["batch_size"]:
            col.warn(f"{fp}:[stream].session_max_len: session_max_len = "
                     f"{stream.session_max_len} > run.batch_size = "
                     f"{run['batch_size']}，超长会话将被 M10 硬切并打 "
                     f"session_split 标（S21）")
        if segment.strategy == "rules" and segment.noise_filter:
            col.warn(f'{fp}:[segment].noise_filter: strategy = "rules" 时 '
                     f"noise_filter 不生效（噪声标记与 min_len 仅 llm/hybrid 策略"
                     f"生效）——要过滤噪声帧请切 strategy")
        if stitch.enabled and segment.strategy == "rules":
            # v1.9 T17 advisory: rules segmentation feeds coarse whole-session
            # cuts into the stitch pool — legal but usually unintended
            col.warn(f'{fp}:[stitch].enabled: segment.strategy = "rules" 时分段'
                     f"无 LLM 精化，缝合输入为整会话粗段——要按任务粒度缝合请将 "
                     f'strategy 切为 "llm" 或 "hybrid"')
        if stitch_provided["non_switch_keys"] and not stitch.enabled:
            # v1.9 T17: the parked-list warning below lives in the segment-off
            # branch — this combination (payload while stitch off, segment on)
            # gets its own warning (sequence_frames precedent)
            col.warn(f"{fp}:[stitch].enabled: stitch.enabled = false，[stitch] "
                     f"其余键不会生效，已忽略（留配置、关开关合法）")
        # S29 combo advisory: only when the EFFECTIVE rubric is the trajectory
        # rubric (incl. the empty-selector stream resolution) — an explicit
        # default:text/ui/inline choice scores by its own criteria and must
        # not be told it is doing trajectory scoring.
        if (quality.enabled and not extract.enabled
                and selector == "default:trajectory"):
            col.warn(f"{fp}:[quality].enabled: segment.enabled = true 且 "
                     f"extract.enabled = false，轨迹打分（default:trajectory）将按"
                     f"帧摘要评估「帧间变化」而非结构化动作序列——要按动作序列打分"
                     f"请启用 [extract]")
    else:
        # no-op warnings (R8 family): parked stream-family config is legal —
        # warn once, naming the ignored tables
        parked = []
        if stream_provided["section"]:
            parked.append("[stream]")
        if segment_provided["non_switch_keys"]:
            parked.append("[segment]")
        if stitch_provided["non_switch_keys"] and not stitch.enabled:
            parked.append("[stitch]")                  # v1.9 (T17)
        if extract_provided["non_switch_keys"] and not extract.enabled:
            parked.append("[extract]")
        if parked:
            col.warn(f"{fp}:[segment].enabled: segment.enabled = false，"
                     f"{'、'.join(parked)} 不会生效，已忽略（留配置、关开关合法）")
        if sequence_frames_provided:
            col.warn(f"{fp}:[annotate].sequence_frames: segment.enabled = false，"
                     f"sequence_frames 仅序列标注（stream 模式）生效，不会生效")

    # ── v1.11 — context budget & vision derivation (spec 3.1.4 上下文预算行) ─
    # V2 (V27② raw-section probe): the removed key gets a DIRECTED error with
    # migration guidance — never the unknown-key forward-compat WARN.
    if segment_provided["use_vision"]:
        col.error(f"{fp}:[segment].use_vision: segment.use_vision 已于 v1.11 移除："
                  f"窗口是否附图由 segment.llm 所指 profile 的 supports_vision 自动"
                  f"决定；如需纯文本裁决，请将 segment.llm 指向纯文本 profile（V2）")

    # V1: freeze the parse product (mode_resolved precedent) — every consumer
    # below (V5/V9) and the assembled ResolvedConfig read the frozen value.
    prof_seg = llm_profiles.get(segment.llm)
    segment = replace(segment, vision_resolved=(
        modality == "ui" and segment.enabled
        and segment.strategy in ("llm", "hybrid")
        and prof_seg is not None and prof_seg.supports_vision))

    # V5 (S28 sibling): the Anthropic ">20 images ∧ any edge >2000px" 400
    # hard-reject domain, segment multi-image window flavor (the S28 WARN
    # above covers annotate.sequence_frames only). Default window = 20 sits
    # just inside the boundary — never fires untouched.
    if (segment.vision_resolved and segment.window > 20
            and prof_seg is not None and prof_seg.max_image_px > 2000):
        col.warn(f"{fp}:[segment].window: window = {segment.window} > 20 且 "
                 f"vision_resolved 生效、被 segment 引用的 profile "
                 f"[llm.{segment.llm}] max_image_px = {prof_seg.max_image_px} "
                 f"> 2000——Anthropic 对 >20 图请求硬拒任一边 >2000px 的图"
                 f"（400，非自动缩放），请将 max_image_px 改为 ≤ 2000 或降回 "
                 f"window ≤ 20（V5）")

    # V6: one WARN per stage-enabled-referenced profile without a declared
    # window (budget OFF there; non-blocking, includes a declaration hint).
    for name in sorted(referenced):
        prof_r = llm_profiles.get(name)
        if prof_r is not None and prof_r.context_window == 0:
            col.warn(f"{fc}:[llm.{name}].context_window: 被启用阶段引用但未声明"
                     f"（0 = 该 profile 上下文预算关闭）——建议按部署实效窗口声明"
                     f"（如 context_window = 131072；欠声明恒安全，只多裁不溢出，"
                     f"V6/V26）")
    if dedup.semantic and dedup.semantic_embedding in embedding_profiles:
        prof_e2 = embedding_profiles[dedup.semantic_embedding]
        if prof_e2.context_window == 0:
            col.warn(f"{fc}:[embedding.{prof_e2.name}].context_window: 被启用阶段"
                     f"引用但未声明（0 = 该 profile 嵌入预算关闭）——建议按部署"
                     f"实效窗口声明（欠声明恒安全，V6/V15）")

    # V9 static guard: when the segment stage runs under a declared budget,
    # the worst-case guaranteed packing size must fit floor frames — floor 3
    # under verify repair (the fixed 3-frame member-reclaim re-judgment
    # window, F14: policy="drop" builds no reclaim window and keeps floor 2).
    if (segment.enabled and segment.strategy in ("llm", "hybrid")
            and prof_seg is not None and prof_seg.context_window > 0):
        w_min = budget.min_window(
            SimpleNamespace(segment=segment, llm_profiles=llm_profiles))
        floor = 3 if (verify.enabled and verify.policy == "repair"
                      and segment.enabled) else 2
        if w_min < floor:
            col.error(f"{fp}:[segment].window: 预算最坏保证装填量 w_min = {w_min} "
                      f"< floor = {floor}（profile [llm.{segment.llm}] "
                      f"context_window = {prof_seg.context_window}）——须静态保证"
                      f"任意帧装得进 {floor} 帧窗（verify repair 复裁窗恒为 3 帧），"
                      f"请调大 context_window、缩小 segment.digest_max_chars 或"
                      f"更换 profile（V9）")
        elif w_min == floor:
            col.warn(f"{fp}:[segment].window: 预算最坏保证装填量 w_min = {w_min} "
                     f"== floor——退化形态：每帧皆接缝、逐帧双裁决，窗数放大"
                     f"（200 帧满长会话至多 199 窗 ≈ 默认 20 窗形态的 18 倍调用量，"
                     f"V9）")

    # V13③ static system-side precheck: per enabled stage on budget-declared
    # profiles, the un-trimmable prompt parts (V22 frozen template head +
    # instruction/rubric/class table/schema/few-shot from ResolvedConfig) must
    # leave room — est ≥ input_budget is a mathematical certainty of failure
    # (CONFIG_ERROR); est > 50% halves the per-record share (WARN, A5).
    schema_text = json.dumps(user_schema, ensure_ascii=False) if user_schema else ""
    rubric_text = "\n".join(
        f"{c.key}\n{c.description}\n{c.pairwise_prompt}"
        + ("\n" + "\n".join(c.pointwise_levels) if quality.mode == "pointwise" else "")
        for c in rubric.criteria)
    class_table_text = "\n".join(
        f"{c.name}\n{c.description}\n" + "\n".join(c.examples)
        for c in classify.classes)
    fewshot_text = "\n".join(
        f"{ex.input}\n{json.dumps(ex.output, ensure_ascii=False)}"
        for ex in annotate.examples)
    static_checks: list[tuple[str, tuple[str, ...], int]] = []
    if segment.enabled and segment.strategy in ("llm", "hybrid"):
        static_checks.append(("segment", (segment.llm,),
                              budget.TEMPLATE_HEAD_TOKENS["segment"]
                              + budget.est_text(segment.context)))
    if stitch.enabled:
        static_checks.append(("stitch", (stitch.llm,),
                              budget.TEMPLATE_HEAD_TOKENS["stitch"]
                              + budget.est_text(stitch.context)))
    if classify.enabled:
        static_checks.append(("classify", (classify.llm,),
                              budget.TEMPLATE_HEAD_TOKENS["classify"]
                              + budget.est_text(classify.instruction)
                              + budget.est_text(class_table_text)))
    if extract.enabled:
        static_checks.append(("extract", (extract.llm,),
                              budget.TEMPLATE_HEAD_TOKENS["extract"]
                              + budget.est_text(extract.instruction)))
    if quality.enabled:
        q_profiles = (quality.judges if quality_judges_active else (quality.llm,))
        static_checks.append(("quality", tuple(q_profiles),
                              budget.TEMPLATE_HEAD_TOKENS["quality"]
                              + budget.est_text(rubric_text)))
    if annotate.enabled:
        static_checks.append(("annotate", (annotate.llm,),
                              budget.TEMPLATE_HEAD_TOKENS["annotate"]
                              + budget.est_text(annotate.instruction)
                              + budget.est_text(schema_text)
                              + budget.est_text(fewshot_text)))
    if generate.enabled:
        static_checks.append(("generate", tuple(generate.llms),
                              budget.TEMPLATE_HEAD_TOKENS["generate"]
                              + budget.est_text(generate.instruction)))
    if verify.enabled:
        v_profiles = verify.judges if verify.judges else (verify.llm,)
        static_checks.append(("verify", tuple(v_profiles),
                              budget.TEMPLATE_HEAD_TOKENS["verify"]
                              + budget.est_text(verify.extra_criteria)
                              + budget.est_text(annotate.instruction)))
    for sect, prof_names, est_static in static_checks:
        for name in prof_names:
            prof_s = llm_profiles.get(name)
            if prof_s is None or prof_s.context_window <= 0:
                continue
            ib = budget.input_budget(prof_s)
            if est_static >= ib:
                col.error(f"{fp}:[{sect}]: 静态系统侧提示部件估算 {est_static} "
                          f"token ≥ 输入预算 {ib} token（profile [llm.{name}]，"
                          f"context_window = {prof_s.context_window}）——任何记录"
                          f"都装不下（V13③），请精简 instruction/rubric/类表/"
                          f"schema/few-shot 或改用更大窗口的 profile")
            elif est_static * 2 > ib:
                col.warn(f"{fp}:[{sect}]: 静态系统侧提示部件估算 {est_static} "
                         f"token 超过输入预算 {ib} token 的 50%（profile "
                         f"[llm.{name}]）——单记录可用空间不足半，质量可能退化"
                         f"（V13③）")

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

    # ── v1.10 console: CLI precedence + mode_resolved freeze (spec 3.1.4 ─
    # console row / §7.7, U21/U25). --console values are pre-validated by
    # argparse choices; explicit rich = CLI --console rich OR the [console].mode
    # key literally present in the TOML with value "rich".
    effective_mode: str = cli.console if cli.console is not None else console.mode
    explicit_rich = cli.console == "rich" or console_rich_explicit
    if tool.log_format == "jsonl":
        # §7.7 铁律: stderr 逐行可 json.loads — jsonl forces plain and CANNOT be
        # overridden by explicit rich; the explicit conflict warns exactly once.
        mode_resolved: str = "plain"
        if explicit_rich:
            col.warn('console: log_format="jsonl" 强制 plain——显式 rich 不生效'
                     "（stderr 逐行可解析铁律，7.7）")
    elif effective_mode == "plain":
        mode_resolved = "plain"
    elif effective_mode == "rich":
        # Explicit rich is honored even without a TTY (CI ANSI-recording
        # scenario, §7.7) — only importability can demote it.
        if _find_spec("rich") is not None:
            mode_resolved = "rich"
        else:
            mode_resolved = "plain"
            col.warn("console: rich 不可导入，降级 plain")
    else:  # auto — the §7.7 decision chain over terminal capability probes
        mode_resolved = _auto_console_mode(
            isatty=sys.stderr.isatty(),
            log_format=tool.log_format,
            term=os.environ.get("TERM"),
            rich_importable=_find_spec("rich") is not None,
        )

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
        console=replace(
            console,
            mode=effective_mode,             # type: ignore[arg-type] # CLI > config (2.5)
            mode_resolved=mode_resolved,     # type: ignore[arg-type] # frozen verdict (U21)
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
        stream=stream,
        dedup=dedup,
        segment=segment,
        stitch=stitch,
        extract=extract,
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

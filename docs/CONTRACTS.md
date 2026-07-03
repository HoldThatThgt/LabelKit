# LabelKit — Cross-Module Interface Contract (CONTRACTS.md)

**Status: FROZEN.** This document is the single interface contract for parallel implementation of
M1–M12 + CLI by independent engineers. It is derived from the design spec v1.4 (`spec/*.md`), which
remains the authority for *algorithms and behavior*; this document is the authority for *names,
signatures, types, defaults, file formats, and prompt text*. Where the spec left a signature or
format implicit, the decision is frozen here and tagged **[FROZEN HERE]** (all such decisions are
also listed in §12). Any deviation requires editing this file first.

Ground rules for every implementer:

- Python ≥ 3.11. Deps: `httpx`, `jsonschema`, `datasketch`, `Pillow`, `imagehash`, `json_repair`,
  `numpy`, stdlib `tomllib`. Nothing else.
- Code identifiers, comments, docstrings-of-record: English. LLM prompt templates: the exact
  Chinese text given in §10 of this document (copied from the spec verbatim).
- Do not rename any field, key, event, or error code defined here. Tests assert exact strings.
- Import discipline (no cycles): `types.py` and `errors.py` import nothing from `labelkit`;
  `config/model.py` imports nothing from `labelkit` except `types` if needed; `llm_client.py`
  imports `types`, `errors`, `config.model`, `obslog`; `schema_engine.py` imports `llm_client`,
  `errors`, `obslog`; `stage.py` imports the above under `typing.TYPE_CHECKING` only; operator
  modules (`ingest/dedup/quality/annotate/generate/verify/emitter`) import service modules and
  `types`/`stage`, **never each other** — with the single sanctioned exception that
  `verify.py` imports the public repair hooks from `annotate.py` (§7.4; used per §7.6).

---

## 1. Package layout and ownership

```
labelkit/
  __init__.py                 # __version__ = "1.0.0"; TOOL_VERSION = f"labelkit/{__version__}"
  cli.py                      # Entry layer: run | validate | rubric        → owner E13
  errors.py                   # Exception hierarchy + exit codes + ErrorKind → shared, frozen here
  types.py                    # Ch.4 shared data types                       → shared, frozen here
  stage.py                    # Stage protocol + RunContext                  → shared, frozen here
  config/
    __init__.py               # re-exports: load, default_rubric, ResolvedConfig
    model.py                  # all config dataclasses (§5)                  → M1 owner (E1)
    loader.py                 # load(), default_rubric(), validation         → M1 owner (E1)
  ingest.py                   # M2: Ingestor, IngestPlan, IngestReport       → E2
  dedup.py                    # M3: DedupStage, DedupIndex                   → E3
  quality.py                  # M4: QualityStage, fit_bradley_terry          → E4
  annotate.py                 # M5: AnnotateStage, build_annotate_prompt,
                              #     annotate_record, RepairContext           → E5
  generate.py                 # M6: GenerateStage, generate_all              → E6
  verify.py                   # M7: VerifyStage                              → E7
  schema_engine.py            # M8: SchemaEngine + internal schemas          → E8
  llm_client.py               # M9: LLMClient, Part/Message/PromptBundle/
                              #     LLMResponse, ProfileUsage, ProbeResult   → E9
  orchestrator.py             # M10: Orchestrator, RunSummary                → E10
  emitter.py                  # M11: Emitter, meta assembly, report writer   → E11
  obslog.py                   # M12: TraceEvent, EventLog, MetricsSink,
                              #     setup_logging, event-name constants      → E12
  data/rubrics/
    default_text.toml         # already written — do not modify
    default_ui.toml           # already written — do not modify
tests/                        # pytest; each owner ships tests for their module
```

`errors.py`, `types.py`, `stage.py` and `config/model.py` are **copy-paste from this document**
(sections 3–6). They are shared code; whoever lands first commits them verbatim, nobody edits them
afterwards without updating this file.

---

## 2. Architecture recap (normative)

Four layers (spec §2.2): CLI → M10 orchestrator → operator stages (M2 ingest, M3 dedup, M4 quality,
M5 annotate, M6 generate, M7 verify, M11 emitter) → services (M1 config, M8 schema engine, M9 LLM
client, M12 obslog). Operators depend only on services and the shared types — never on each other
(exception: verify→annotate repair hook, §7.4/§7.6).

Pipeline order per batch (process mode):
`dedup → quality → generate(off-path, returns sub-batch) → annotate → verify → emit`.
Generation sub-batches re-enter the queue as new batches and run
`dedup → quality → annotate → verify → emit` (no generate; single-pass, no recursion).
`generate_only` mode (v1.4): no M2; `GenerateStage.generate_all()` produces all Records up front,
they are split by `run.batch_size`, and each batch runs `dedup → quality → annotate → verify → emit`
(quality/annotate individually optional per switches).

Statuses: `active | dropped_dup | dropped_lowq | dropped_verify | failed`. Stages never delete list
elements; they flip `status` and attach evidence.

---

## 3. `labelkit/types.py` — verbatim

```python
"""Shared data types (spec ch.4). Frozen contract — do not edit without updating CONTRACTS.md."""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

Status = Literal[
    "active",          # alive, keeps flowing
    "dropped_dup",     # M3 judged duplicate
    "dropped_lowq",    # M4 below quality gate
    "dropped_verify",  # M7 verdict fail with policy=drop (or repair budget exhausted)
    "failed",          # processing error (irreparable schema / provider retries exhausted ...)
]


@dataclass(frozen=True)
class RecordRef:
    source_file: str                       # path relative to run.input ("" for generated records)
    line_no: int | None                    # text modality: 1-based line number
    pair_index: int | None                 # UI modality: file-pair index
    generated_from: tuple[str, ...]        # process-mode generated sample: seed record ids;
                                           # everything else (incl. generate_only samples): ()
                                           # — synthetic-ness is judged by `generator`, not this (v1.4)
    generator: Mapping | None = None       # generated records: {"llm": <profile>, "style": <name>|None}
                                           # non-generated records: None


@dataclass(frozen=True)
class ImageRef:
    path: Path
    format: Literal["png", "jpeg"]         # ".jpg"/".jpeg" both map to "jpeg"
    size_bytes: int

    def load_base64(self, max_px: int) -> tuple[str, str]:
        """Load from disk at call time. If the longer edge exceeds max_px, downscale
        proportionally (Pillow) before encoding. Returns (media_type, b64) where media_type is
        "image/png" | "image/jpeg". Bytes are not cached — used and discarded (spec §2.6)."""
        ...


@dataclass(frozen=True)
class UINode:
    node_id: str
    parent_id: str | None
    depth: int
    role: str                              # widget role normalized from class/type
    text: str
    content_desc: str
    bounds: tuple[int, int, int, int]      # (l, t, r, b) pixels
    visible: bool
    extra: Mapping[str, str]               # non-whitelisted source fields, values stringified


@dataclass(frozen=True)
class UITree:
    nodes: tuple[UINode, ...]              # depth-first order

    def serialize(self, max_chars: int | None = None, quantize_px: int = 0) -> str:
        """Canonical linearization (spec §4.3), shared by M3 dedup (quantize_px =
        dedup.bounds_quantize_px) and M5 prompts (quantize_px = 0, max_chars =
        input.ui_tree_max_chars).

        Rules (exact):
        - Traverse `nodes` in stored (depth-first) order; skip nodes with visible == False.
        - One line per node, joined with "\\n", no trailing newline:
            line = ("  " * depth) + role
                   + (f' "{text}"' if text else "")
                   + (f' desc="{content_desc}"' if content_desc else "")
                   + f" [{l},{t},{r},{b}]"
                   + "".join(f" {k}={v}" for k, v in extra.items() if v)
          (extra in insertion order; indentation is TWO spaces per depth level — matches the
           worked examples in spec 3.2.7/3.9.4 [FROZEN HERE, see §12].)
        - If quantize_px > 0, each coordinate is floor-divided first: c = c // quantize_px.
        - If max_chars is not None and the full output exceeds it: keep the longest prefix of
          whole lines whose joined length (incl. "\\n" separators and the marker line below)
          ≤ max_chars, then append a final line "…(truncated N nodes)" where N = number of
          visible nodes omitted. [FROZEN HERE]
        """
        ...


@dataclass(frozen=True)
class Record:
    id: str                                # sha256 hex prefix [:16]; rule per modality, see below
    modality: Literal["text", "ui"]
    text: str | None                       # text modality: extracted text; UI modality: None
    raw: Mapping | None                    # text modality: original line object; UI: None
    ui_tree: UITree | None
    image: ImageRef | None
    ref: RecordRef
```

**Record id rules (M2/M6, normative):**
- text modality: `sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]` where
  `canonical_json(x) = json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.
- UI modality: `sha256(uitree_file_bytes + image_file_bytes).hexdigest()[:16]`.
- generated records (M6): `raw = {input.text_field: sample_text}`, then the text rule.

```python
@dataclass(frozen=True)
class DedupInfo:
    kind: Literal["unique", "exact", "near_text", "near_image", "near_both", "near_semantic"]
    cluster_key: str                       # exact-dedup key ([:16] hex) of the cluster head;
                                           # unique records carry their own key
    kept_id: str | None                    # duplicates: id of the retained record; unique: None


@dataclass(frozen=True)
class QualityScore:
    criterion: str                         # rubric criterion key, or "__aggregate__"
    score: float | None                    # [0,1] normalized; None = unscored (all judgments failed)
    mode: Literal["pairwise_bt", "pointwise"]
    detail: Mapping                        # pairwise: {comparisons, wins, ties, log_theta}
                                           # pointwise: {raw_score (0-5), reason}
                                           # __aggregate__: {}


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":          # [FROZEN HERE]
        return Usage(self.prompt_tokens + other.prompt_tokens,
                     self.completion_tokens + other.completion_tokens)

    def __radd__(self, other: object) -> "Usage":          # [FROZEN HERE]
        # Supports `sum(usage_list)`: sum's implicit start is int 0.
        if other == 0:
            return self
        return NotImplemented


@dataclass(frozen=True)
class Annotation:
    output: Mapping                        # object that PASSED the user schema (L2)
    model: str                             # provider model string of the annotating profile
    attempts: int                          # 1 + number of L3 repair calls
                                           # (self-consistency: sum over the SUCCESSFUL samples)
    usage: Usage                           # tokens of first call + repair calls (successful samples if SC)
    sc: Mapping | None = None              # self-consistency only: {"n": int, "agreement_ratio": float}
                                           # [FROZEN HERE: carried here so M11 can write _meta]


@dataclass(frozen=True)
class VerificationResult:
    verdict: Literal["pass", "fail"]
    rounds: int                            # judged rounds incl. the first (pass on first review = 1)
    critiques: tuple[Mapping, ...]         # accumulated over rounds, in order:
                                           # {"aspect": str, "opinion": str[, "judge": str]}


@dataclass(frozen=True)
class StageError:
    stage: str                             # stage name that produced the error
    kind: str                              # error classification code (§7.6 / errors.ErrorKind)
    message: str
    retryable: bool


@dataclass
class PipelineItem:                        # the ONLY mutable envelope; lifetime = one batch
    record: Record
    status: Status = "active"
    dedup: DedupInfo | None = None
    scores: dict[str, QualityScore] = field(default_factory=dict)
    annotation: Annotation | None = None
    verification: VerificationResult | None = None
    errors: list[StageError] = field(default_factory=list)
```

Notes binding on all implementers:

- `QualityScore.score` is `float | None` — the spec's `on_unscored` path requires representing
  "score = null" (spec 3.4.3 判定失败 row, §6.3 example semantics). **[FROZEN HERE]**
- `Annotation.sc` is an additive v1.2 field needed to carry `{n, agreement_ratio}` from M5 to M11
  (`_meta.annotation.sc`, spec 3.5.2/6.3). **[FROZEN HERE]**
- Everything except `PipelineItem` is `frozen=True`. No module mutates a `Record`.

---

## 4. `labelkit/errors.py` — verbatim

```python
"""Exception hierarchy (spec §4.3) and error classification codes (spec §7.6)."""
from __future__ import annotations

import enum


class LabelKitError(Exception):
    """Base for all tool errors."""


class ConfigError(LabelKitError):
    """M1. Aggregates ALL validation errors (never just the first). CLI exit code 2."""
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class InputError(LabelKitError):
    """M2, raised when an input.* policy is 'fail' (or no valid record exists /
    path missing at run start). Process mode only. CLI exit code 3."""
    def __init__(self, message: str):
        super().__init__(message)


class ProviderRetryableError(LabelKitError):
    """M9: retryable provider error with retries exhausted. Record-level → status='failed'."""
    def __init__(self, message: str, profile: str, retries: int):
        self.profile = profile
        self.retries = retries
        super().__init__(message)


class ProviderFatalError(LabelKitError):
    """M9: non-retryable provider error (401/403/400/404, dims mismatch). Feeds the circuit
    breaker; a streak >= run.fatal_error_threshold ends the run with exit code 4."""
    def __init__(self, message: str, profile: str, status_code: int | None = None):
        self.profile = profile
        self.status_code = status_code
        super().__init__(message)


class SchemaViolation(LabelKitError):
    """M8: L3 budget exhausted, object still invalid. Record-level → status='failed',
    kind='schema_violation'."""
    def __init__(self, errors: list[str], raw_last_output: str):
        self.errors = errors                  # rendered violations: "<json-pointer>: <message>"
        self.raw_last_output = raw_last_output
        super().__init__("; ".join(errors))


class InternalError(LabelKitError):
    """Invariant breakage (e.g. M11 final validate_only failure). Record-level → 'failed',
    kind='internal_error'; stack goes to stderr log at debug level."""


class CircuitBreakerTripped(LabelKitError):
    """Raised by LLMClient once MetricsSink.circuit_broken is set; Orchestrator converts it
    to a fatal run end (exit 4). [FROZEN HERE]"""


# ── CLI exit codes (spec §2.4) ─────────────────────────────────────────────
EXIT_OK = 0              # run completed (rejects allowed)
EXIT_STRICT = 1          # completed but --strict violated (rejects exist), or report write failed
EXIT_CONFIG = 2          # ConfigError
EXIT_INPUT = 3           # InputError (process mode only; generate_only never returns 3)
EXIT_FATAL = 4           # provider auth failure / circuit breaker / output path unwritable


class ErrorKind(str, enum.Enum):
    """StageError.kind values (spec §7.6). Compare/serialize by .value."""
    BAD_INPUT_LINE = "bad_input_line"                        # M2, record-level
    MISSING_PAIR = "missing_pair"                            # M2, record-level
    INDEX_CONFLICT = "index_conflict"                        # M2, record-level
    IMAGE_TOO_LARGE = "image_too_large"                      # M2, record-level
    IMAGE_DECODE_ERROR = "image_decode_error"                # M3 skip pHash; M5/M7 → failed
    JUDGMENT_INVALID = "judgment_invalid"                    # M4, comparison-level → counts as tie
    SCHEMA_VIOLATION = "schema_violation"                    # M8 L3 exhausted → failed → rejects
    CALLBACK_VIOLATION = "callback_violation"                # v1.5: L3 exhausted, remaining
                                                             # violations all from output.validator
    PROVIDER_RETRYABLE_EXHAUSTED = "provider_retryable_exhausted"  # M9 → failed, feeds breaker window
    PROVIDER_FATAL = "provider_fatal"                        # M9 run-level, feeds breaker directly
    INTERNAL_ERROR = "internal_error"                        # any unexpected exception
```

Exception → exit-code mapping is implemented **only** in `cli.py` (§7.12). No module calls
`sys.exit`.

---

## 5. `labelkit/stage.py` — verbatim

```python
"""Stage protocol (spec §4.3) and RunContext (spec §3.10.3). Frozen contract."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from labelkit.config.model import ResolvedConfig
    from labelkit.llm_client import LLMClient
    from labelkit.schema_engine import SchemaEngine
    from labelkit.obslog import MetricsSink
    from labelkit.types import PipelineItem


@dataclass
class RunContext:
    """Context handed to every stage.run() invocation. Constructed by M10 orchestrator,
    ONE PER (batch, stage) INVOCATION, because rng is derived per batch and stage.
    Exactly the six fields of spec 3.10.3 — spec 3.12.3 explicitly forbids extending this
    signature; run_id/run_started_at travel via the MetricsSink/Emitter/Orchestrator
    constructors instead (§7.9–§7.11)."""
    cfg: ResolvedConfig
    llm: LLMClient
    schema_engine: SchemaEngine
    metrics: MetricsSink
    rng: random.Random            # random.Random(f"{cfg.run.seed}:{batch_no}:{stage_name}")
    batch_no: int                 # 1-based; run-level events use 0


class Stage(Protocol):
    name: str

    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]:
        """契约：① 只处理 status=='active' 的项；② 不删除列表元素（只改 status）；
           ③ generate 例外——返回新增子批（原批元素不修改）；④ 单条失败不得抛出到批层面，
           必须落入 item.errors 并置 status='failed'。"""
        ...
```

Binding rules:

- **RNG ownership.** Only the orchestrator seeds RNGs. Derivation string is exactly
  `f"{cfg.run.seed}:{batch_no}:{stage_name}"` (spec 3.10.3). Stages use `ctx.rng` for ALL
  randomness (pair sampling, A/B order, seed sampling, style/llm draws) and never call
  `random.*` module functions or create their own `Random`. `generate_only` pre-draw uses
  `random.Random(f"{seed}:0:generate")` (batch_no fixed at 0, spec 3.10.3).
- All stages except `generate` return the same list object they received. `generate.run` returns a
  **new** list of new `PipelineItem`s (the sub-batch) and does not touch the input list.
- Non-generate stages: return value must be the input list (callers may rely on identity).
- A stage must catch every per-record exception, append
  `StageError(stage=self.name, kind=..., message=..., retryable=...)` to `item.errors`, set
  `status="failed"`, emit the `error` trace event, and continue. Only `CircuitBreakerTripped`,
  `KeyboardInterrupt`/`CancelledError` may escape a stage.

---

## 6. `labelkit/config/` — M1

### 6.1 `config/model.py` — verbatim dataclasses

Every field name, type and default below mirrors the spec §5.1/§5.2/§5.3 tables exactly.
`None` means "absent/optional" unless stated. All arrays become tuples (immutability).

```python
"""Typed, frozen mirror of config.toml + project.toml + CLI overrides (spec ch.5)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping


# ── config.toml side ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolConfig:
    log_level: str = "info"                       # debug|info|warn|error; overridden by --log-level
    log_format: Literal["text", "jsonl"] = "text" # jsonl disables the progress bar (spec §7.7)


@dataclass(frozen=True)
class LLMProfile:
    name: str                                     # the [llm.<name>] key            [FROZEN HERE]
    provider: Literal["openai_compatible", "anthropic"]
    base_url: str
    model: str
    api_key_env: str
    max_concurrency: int = 8
    timeout_s: int = 120
    max_retries: int = 5
    retry_base_delay_s: float = 1.0
    supports_structured_output: bool = False
    supports_vision: bool = False
    max_output_tokens: int = 4096
    temperature: float = 0.0
    max_image_px: int = 2048
    price_per_mtok_in: float | None = None
    price_per_mtok_out: float | None = None
    api_key: str = field(default="", repr=False)  # resolved from env by M1; NEVER logged
                                                  # [FROZEN HERE]


@dataclass(frozen=True)
class EmbeddingProfile:
    name: str                                     # the [embedding.<name>] key      [FROZEN HERE]
    base_url: str
    model: str
    api_key_env: str
    provider: Literal["openai_compatible"] = "openai_compatible"
    max_concurrency: int = 8
    timeout_s: int = 60
    max_retries: int = 5
    retry_base_delay_s: float = 1.0               # same backoff mechanism as llm.* [FROZEN HERE]
    dims: int | None = None                       # if set, embed() validates returned dims
    api_key: str = field(default="", repr=False)  # resolved from env by M1


# ── project.toml side ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunConfig:
    output: str
    modality: Literal["text", "ui"]
    input: str | None = None                      # required in process mode (CLI --input may fill);
                                                  # MUST be absent in generate_only
    mode: Literal["process", "generate_only"] = "process"
    batch_size: int = 256                         # = QuRating comparison-pool size
    seed: int = 0
    fatal_error_threshold: int = 20


@dataclass(frozen=True)
class InputConfig:
    text_field: str = "text"                      # dotted path (e.g. "conversation.turns")
    on_bad_line: Literal["skip", "fail"] = "skip"
    on_missing_pair: Literal["skip", "fail"] = "skip"
    on_index_conflict: Literal["skip", "fail"] = "fail"
    max_image_mb: int = 20
    ui_tree_max_chars: int = 30000


@dataclass(frozen=True)
class DedupConfig:
    enabled: bool = True
    scope: Literal["global", "batch"] = "global"
    minhash_threshold: float = 0.85
    minhash_num_perm: int = 128
    ngram: int = 5
    image_phash_max_distance: int = 8
    ui_dup_requires: Literal["both", "tree", "image"] = "both"
    bounds_quantize_px: int = 4
    semantic: bool = False
    semantic_embedding: str | None = None         # required iff semantic=True; [embedding.*] name
    semantic_threshold: float = 0.95


@dataclass(frozen=True)
class QualityConfig:
    enabled: bool = True
    mode: Literal["pairwise", "pointwise"] = "pairwise"
    llm: str = "default"
    rounds: int = 4                               # pairwise k
    criteria_per_call: Literal["all", "single"] = "all"
    threshold: float | None = None                # absent = score only, no filtering
    selection: Literal["threshold", "top_ratio"] = "threshold"
    top_ratio: float | None = None                # (0,1]; required iff selection="top_ratio"
    judges: tuple[str, ...] = ()                  # empty = single judge (quality.llm); else odd count
    both_orders: bool = False
    on_unscored: Literal["keep", "drop"] = "keep"
    rubric: str = ""                              # "default:text"|"default:ui"|"inline";
                                                  # "" = auto by modality (M1 resolves)
    judgment_reasons: bool | str = "auto"         # "auto" | True | False


@dataclass(frozen=True)
class GenerateStyle:
    name: str                                     # unique within the table
    prompt: str                                   # non-empty


@dataclass(frozen=True)
class GenerateConfig:
    enabled: bool = False
    llms: tuple[str, ...] = ("default",)
    instruction: str = ""                         # required iff enabled
    mixture: Literal["round_robin", "weighted"] = "round_robin"
    weights: tuple[float, ...] = ()               # required iff mixture="weighted"; len == len(llms)
    styles: tuple[GenerateStyle, ...] = ()
    num_per_record: int = 2
    seeds_per_call: int = 3
    num_per_call: int = 4
    seed_min_score: float | None = None           # None = auto (quality.threshold, else batch median)
    temperature: float = 0.9
    sample_validator: str | None = None           # v1.5 plan-A hook: "module:function",
                                                  # fn(text) -> list[str]; per-sample filter
                                                  # BEFORE the similarity filter (spec 3.6.2)
    seed_examples: tuple[str, ...] = ()           # generate_only seed-pool form only
    standalone_count: int | None = None           # generate_only seedless form only; mutually
                                                  # exclusive with seed_examples


@dataclass(frozen=True)
class FewShotExample:
    input: str
    output: Mapping                               # must pass the user schema (M1 validates)


@dataclass(frozen=True)
class AnnotateConfig:
    enabled: bool = True
    llm: str = "default"
    instruction: str = ""                         # required iff enabled
    examples: tuple[FewShotExample, ...] = ()
    self_consistency: int = 0                     # 0 = off; else odd, >= 3
    sc_temperature: float = 0.7


@dataclass(frozen=True)
class VerifyConfig:
    enabled: bool = False
    llm: str = "judge"                            # must exist in [llm.*] iff enabled
    judges: tuple[str, ...] = ()                  # empty = single judge (verify.llm); else odd count
    policy: Literal["drop", "repair"] = "drop"
    max_repair_rounds: int = 1
    extra_criteria: str = ""


@dataclass(frozen=True)
class OutputConfig:
    schema_path: str | None = None                # exactly one of schema_path / schema_inline
    schema_inline: str | None = None
    max_repair_attempts: int = 2                  # schema-engine L3 budget
    repair_llm: str | None = None                 # None = same profile as the caller
    meta_mode: Literal["inline", "sidecar", "none"] = "inline"
    passthrough_fields: tuple[str, ...] = ()
    rejects: Literal["none", "refs", "full"] = "refs"
    validator: str | None = None                  # v1.5 plan-A hook: "module:function",
                                                  # fn(obj, record|None) -> list[str];
                                                  # engine L2.5, user schema only (spec 3.8.2)


@dataclass(frozen=True)
class TraceConfig:
    enabled: bool = False
    path: str = ""                                # M1 resolves "" → "{output_stem}.trace.jsonl"
    channels: tuple[str, ...] = ("quality", "verify", "schema")
                                                  # allowed: ingest|dedup|quality|annotate|verify|schema|llm
    content: Literal["none", "refs", "excerpt", "full"] = "refs"


# ── rubric (appendix A structure, spec §5.3) ───────────────────────────────

@dataclass(frozen=True)
class Criterion:
    key: str                                      # [a-z0-9_]+, globally unique
    description: str
    pairwise_prompt: str
    weight: float = 1.0                           # > 0
    pointwise_levels: tuple[str, ...] = ()        # exactly 6 entries (levels 0-5) in pointwise mode


@dataclass(frozen=True)
class Rubric:
    name: str
    criteria: tuple[Criterion, ...]


# ── CLI overrides and the aggregate ────────────────────────────────────────

@dataclass(frozen=True)
class CliOverrides:
    input: str | None = None
    output: str | None = None
    limit: int | None = None
    dry_run: bool = False
    strict: bool = False
    log_level: str | None = None


@dataclass(frozen=True)
class ResolvedConfig:
    tool: ToolConfig
    llm_profiles: Mapping[str, LLMProfile]        # key = profile name
    embedding_profiles: Mapping[str, EmbeddingProfile]
    run: RunConfig
    input: InputConfig
    dedup: DedupConfig
    quality: QualityConfig
    generate: GenerateConfig
    annotate: AnnotateConfig
    verify: VerifyConfig
    output: OutputConfig
    trace: TraceConfig
    rubric: Rubric                                # resolved (default pkg or inline)
    user_schema: Mapping                          # parsed dict, meta-schema pre-validated
    limit: int | None                             # CLI --limit
    strict: bool
    dry_run: bool
    config_path: str                              # as given on the CLI
    project_path: str
    config_digest: str                            # "sha256:<hex>" of the raw file bytes [FROZEN HERE]
    project_digest: str
```

`schema_version` (a required top-level int key in BOTH files, spec §5.1/§5.2 row 1) is validated
by §6.3 rule 1 and deliberately **not** mirrored into any dataclass — it is the constant 1 in
this version and carries no runtime information. This is a conscious, recorded deviation from
spec 3.1.2's "typed mirror of ALL keys" wording. **[FROZEN HERE, see §12]**

Resolution duties of M1 (beyond merging): resolve `quality.rubric` default by modality
(`"default:text"` / `"default:ui"`); resolve `trace.path` default; resolve `run.input`/`run.output`
CLI overrides; parse `output.schema_inline`/`schema_path` into `user_schema`; read every
*referenced* profile's `api_key_env` into `LLMProfile.api_key`; `tool.log_level` overridden by
`--log-level`. Precedence: CLI > project.toml > config.toml/built-in defaults.

### 6.2 `config/loader.py` — API (spec 3.1.3, verbatim)

```python
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """Three-source merge + full validation. On failure raises ConfigError(errors: list[str])
    carrying ALL errors (never first-only); CLI exits 2."""

def default_rubric(name: Literal["default:text", "default:ui"]) -> Rubric:
    """Load a packaged default rubric from labelkit/data/rubrics/*.toml
    (importlib.resources)."""
```

Error message format (spec 3.1.5): `"<file>:[section].key: <expected>, got <actual>"`, e.g.
`config.toml:[llm.default].timeout_s: 期望正整数，得到 "abc"`; array-table elements addressed as
`[[rubric.criteria]][N]` with N 1-based. Unknown keys → stderr warning only (forward compat).
Error messages themselves are Chinese where the spec shows Chinese samples; keep the
`<file>:[section].key:` prefix machine-stable.

### 6.3 Validation rules M1 must enforce (complete list, spec 3.1.4 + 2.3.1)

TOML structure:
1. Both files contain `schema_version = 1`. Missing required keys → error; type mismatches per
   §5 tables → error; unknown keys → warning.

Profile references:
2. `quality.llm`, `annotate.llm`, each element of `generate.llms`, `verify.llm` (only when
   `verify.enabled = true` — spec §5.2 footnote †; the default `"judge"` must NOT be required
   to exist when verify is disabled), `output.repair_llm` (when set), each element of
   `quality.judges` and `verify.judges` must exist in `[llm.*]`.
3. `quality.judges` / `verify.judges`: when non-empty, length must be odd.
4. UI modality: every profile used by quality/annotate/verify must have `supports_vision = true`.
5. `dedup.semantic = true` ⇒ `dedup.semantic_embedding` set, exists in `[embedding.*]`, and that
   profile's `api_key_env` is non-empty.

Cross-field constraints (v1.2):
6. `quality.selection = "top_ratio"` ⇒ `quality.top_ratio` required, ∈ (0,1], and
   `quality.threshold` must NOT be set (mutually exclusive).
7. `annotate.self_consistency` is 0 or an odd integer ≥ 3.
8. `generate.mixture = "weighted"` ⇒ `generate.weights` required, every element > 0, and
   `len(weights) == len(llms)`.
9. `[[generate.styles]]`: each `name` unique within the table; each `prompt` non-empty.

Run mode (v1.4):
10. `run.mode = "generate_only"` ⇒ `run.input` absent (also rejecting CLI `--input`),
    `run.modality == "text"`, `generate.enabled == true`; exactly ONE of
    `generate.seed_examples` (non-empty array of non-empty strings) and
    `generate.standalone_count` (≥ 1) is provided.
11. `run.mode = "process"` ⇒ neither `generate.seed_examples` nor `generate.standalone_count`
    may be set.

API keys:
12. For every *referenced* profile, the `api_key_env` environment variable exists and is
    non-empty. Unreferenced profiles are not checked.

User schema:
13. Valid JSON; passes `Draft202012Validator.check_schema`; top-level `"type": "object"`;
    top-level `properties` must not declare the reserved key `_meta`; every `$ref` in a
    schema position must resolve against the schema document itself (the tool never
    retrieves external schema resources at runtime, so an unresolvable ref — remote URI,
    relative path, or dangling local pointer — is a guaranteed runtime failure and is
    rejected at load time; see §12 #23).
14. Exactly one of `output.schema_path` / `output.schema_inline` is provided.
15. Every `annotate.examples[].output` passes the user schema (`SchemaEngine.validate_only`
    semantics; M1 may validate with jsonschema directly to avoid constructing M8).

Rubric:
16. `criteria` non-empty; keys unique and match `[a-z0-9_]+`; every `weight > 0`;
    `quality.mode = "pointwise"` ⇒ every criterion has exactly 6 `pointwise_levels`.
    `quality.rubric = "inline"` ⇒ `[[rubric.criteria]]` must be provided.

Stage combination (spec 2.3.1 ①–④):
17. ① `annotate.enabled` or `quality.enabled` (at least one) — else CONFIG_ERROR.
18. ② `verify.enabled = true` ⇒ `annotate.enabled = true`.
19. ③ `generate.enabled = true` ⇒ `run.modality == "text"`; in process mode additionally
    `quality.enabled = true` (seeds come from the quality gate). In generate_only mode quality
    is optional.
20. ④ = rule 10 above.

Paths:
21. process mode: `run.input` must be set (CLI `--input` counts); `run.output` must not be
    located inside the input directory (best-effort when the input path does not yet exist).
    Both modes: output parent directory exists and is writable.
    NOTE — input EXISTENCE/readability is NOT an M1 check: a missing/unreadable input path
    at run start is M2's job (`Ingestor.scan()`/`records()` raise `InputError` → exit 3,
    spec §2.4), never a `ConfigError` (exit 2).

Warnings (non-blocking): `verify` enabled and `verify.llm`'s `model` equals `annotate.llm`'s
`model` → warn about self-enhancement bias (spec 3.7.2).

---

## 7. Module public APIs

Everything in this section is the complete public surface. Anything not listed is private
(`_`-prefixed) and may not be imported across modules.

### 7.1 M2 — `labelkit/ingest.py`

```python
@dataclass(frozen=True)                            # [FROZEN HERE]
class IngestPlan:
    files: tuple[str, ...]                         # text: .jsonl files (lexicographic by name);
                                                   # UI: all matched files, tree then image per
                                                   # pair, pairs ascending. Paths relative to
                                                   # run.input (as RecordRef.source_file)
    pairs: tuple[tuple[int, str, str], ...]        # UI pairing table (spec 3.2.3 配对表):
                                                   # (index, tree_path, image_path), ascending
                                                   # by index; text modality: ()
    estimated_records: int                         # text: total lines (cheap count); UI: len(pairs)


@dataclass                                         # mutable counters   [FROZEN HERE]
class IngestReport:
    scanned: int = 0                               # lines seen / pair indexes seen
    ingested: int = 0
    bad_input: int = 0                             # bad lines + skipped conflicts + missing pairs
    missing_pair: int = 0                          # UI only
    index_conflict: int = 0                        # UI only
    bad_locations: list[dict] = field(default_factory=list)
                                                   # {"file": str, "line_no": int|None,
                                                   #  "index": int|None, "reason": str}


class Ingestor:
    def __init__(self, cfg: ResolvedConfig): ...

    def scan(self) -> IngestPlan:
        """Scan only, no parsing: file list, pairing table, estimated record count.
        Used by --dry-run and `validate`. Raises InputError if run.input is missing/unreadable
        or (UI, on_index_conflict='fail') a conflict is found."""

    def records(self) -> Iterator[Record]:
        """Lazy Record stream. Parse errors follow input.on_bad_line / on_missing_pair /
        on_index_conflict ('skip' → count + trace event; 'fail' → raise InputError).
        Emits trace events ingest.bad_line / ingest.missing_pair / ingest.index_conflict via
        the metrics sink handed to it (see below)."""

    @property
    def report(self) -> IngestReport: ...
```

Wiring note **[FROZEN HERE]**: `Ingestor` is not a `Stage` and has no `ctx`; the CLI/orchestrator
sets `ingestor.metrics = metrics_sink` (public attribute, default `None`) before calling
`records()` so ingest trace events can be emitted with `batch_no=0`.

Pairing rules (spec 3.2.4, normative): recursive scan; one shared index namespace across
subdirectories; filename patterns `^uitree_(\d+)\.jsonl$` and `^image_(\d+)\.(png|jpg|jpeg)$`
(extension match case-insensitive **[FROZEN HERE]**); index parsed base-10 (leading zeros OK);
≥2 tree files or ≥2 image files for one index = conflict (a `.png` + `.jpg` for the same index is
also a conflict); single-sided index = missing pair. Tree file: JSONL of node objects with the
**spec §6.2** field mapping (source-field precedence lists for node_id/parent_id/role/text/
content_desc/visible, their per-field defaults, and the two accepted bounds forms — `[l,t,r,b]`
array or `"[l,t][r,b]"` string); first-line probe: object containing a `children` array →
nested style, else flat style. Images: magic-number + size check only (`≤ input.max_image_mb`), no full decode.
Text parsing (3.2.5): non-object JSON line = bad line; `input.text_field` dotted path; string hit
used as-is; array/object hit serialized with canonical JSON; miss = bad line; empty lines skipped
silently (not counted as bad).

### 7.2 M3 — `labelkit/dedup.py`

```python
class DedupIndex:
    """In-memory dedup index: exact set[bytes] + datasketch.MinHashLSH + list[(id, phash)]
    (+ list[(id, unit_vec)] when dedup.semantic). scope='batch' → reset per batch."""
    def __init__(self, cfg: DedupConfig, modality: Literal["text", "ui"]): ...   # [FROZEN HERE]

    def probe_and_add(self, rec: Record) -> DedupInfo:
        """Levels ①②(③) probe; on unique, adds the record's keys/signature/phash to the index
        (first-writer-wins). Returns the DedupInfo for the record."""

    @property
    def last_similarity(self) -> float | None:
        """Measured metric of the most recent duplicate verdict: estimated Jaccard (near_text),
        Hamming distance (near_image), or None (exact). For the dedup.duplicate trace event.
        [FROZEN HERE]"""

    # semantic level ④ (only used when cfg.semantic) [FROZEN HERE]
    def semantic_probe(self, vec: list[float]) -> tuple[str, str, float] | None:
        """Returns (kept_id, cluster_key, cosine) of the best match with cosine >= threshold,
        else None. vec must be L2-normalized."""
    def add_vector(self, rec_id: str, cluster_key: str, vec: list[float]) -> None: ...

    def reset(self) -> None:
        """Drop all index state. Called by DedupStage at batch start when scope='batch'."""


class DedupStage(Stage):
    name = "dedup"
    def __init__(self, cfg: DedupConfig, index: DedupIndex): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
```

Behavior (normative, spec 3.3.3): `dedup_text` = text modality: extracted text after NFC
normalization, whitespace-run collapse to single space, strip; UI modality:
`ui_tree.serialize(quantize_px=cfg.bounds_quantize_px)`. Exact key = `sha256(dedup_text)`;
`cluster_key` = first 16 hex of the cluster head's exact key (unique records: own key). Level ②:
character n-grams (n=`ngram`) over the collapsed text, `minhash_num_perm` permutations, LSH at
`minhash_threshold`, verify candidates by signature-estimated Jaccard. Level ③ (UI): 64-bit
pHash, Hamming ≤ `image_phash_max_distance`; matched by linear scan over all kept hashes — a
recorded deviation from spec 3.3.3's 16-bit-prefix bucketing (see §12 #24). UI composite verdict via
`ui_dup_requires` ("both": tree-hit AND image-hit; exact ① always wins unconditionally). Level ④
(semantic): after ①–③, only for records not yet judged duplicates (with "both", a lone ③ hit does
not short-circuit ④); embed `dedup_text` via `ctx.llm.embed(cfg.semantic_embedding, [dedup_text])`
— exactly ONE embed() call per participating record (spec 3.3.3 cost row: 每条参检记录 1 次
embedding 调用), each call metered and retried by M9;
counts as a tree-level hit in the composite; kind `near_semantic` (③+④ together → `near_both`).
Image decode failure → skip pHash for that record, count `report.dedup.image_decode_failures`,
StageError NOT set (record stays active), and the record's composite verdict degrades to
tree-only (`ui_dup_requires` treated as `"tree"` for that record, spec 3.3.4)
**[FROZEN HERE]**; embedding failure after retries →
skip level ④ for that record, count `embedding_failures`. Duplicates: `status="dropped_dup"`,
`item.dedup=DedupInfo(...)`, trace `dedup.duplicate`; survivors get `DedupInfo(kind="unique",...)`.

### 7.3 M4 — `labelkit/quality.py`

```python
class QualityStage(Stage):
    name = "quality"
    def __init__(self, cfg: ResolvedConfig): ...                 # [FROZEN HERE]
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...


def fit_bradley_terry(n_items: int, comparisons: list[tuple[int, int, float]],
                      l2_pseudo: float = 0.1, tol: float = 1e-6, max_iter: int = 200) -> np.ndarray:
    """comparisons: (winner_idx, loser_idx, weight); a tie is split into two entries with
    weight=0.5 each. MM iteration (Hunter 2004) with lambda=l2_pseudo pseudo-matches
    (half-win/half-loss vs a virtual opponent theta=1), renormalized to prod(theta)=1 per
    iteration; stops at max|delta log theta| < tol or max_iter. Returns log-theta array of
    length n_items."""
```

Normative behavior (spec 3.4.3/3.4.4): comparison pool = the batch; k = `rounds` independent
random perfect matchings (shuffle via `ctx.rng`, then pair adjacent; odd survivor sits the round
out); A/B presentation order randomized via `ctx.rng`; judging prompt per §10.2; response
validated by M8 against the judgment schema (§10.7); invalid judgment after repair → tie, count
`judgment_failures`, StageError kind `judgment_invalid` (comparison-level, item stays active).
Multi-judge (`judges` odd, same presented order, per-criterion majority of A/B/tie, no majority →
tie); both_orders (per judge: two orders, consistent → winner, else tie; compose per-judge first,
then across judges). Per-criterion normalization: ascending rank of log θ (ties → average rank),
`score = (rank-1)/(N-1)`, N=1 → 0.5. Aggregate = Σwᵢ·scoreᵢ/Σwᵢ over non-null criteria; all-null →
aggregate `None` → record is "unscored", handled by `on_unscored` ("keep" → stays active with
null scores; "drop" → `dropped_lowq`) **[FROZEN HERE: unscored-drop maps to dropped_lowq]**.
Gate: selection="threshold" & threshold set → aggregate < threshold ⇒ `dropped_lowq`;
selection="top_ratio" → keep top `ceil(top_ratio × n_scored)` by (aggregate desc, id asc);
unscored keepers occupy no slots. Batch of 1 (pairwise mode only): no judging calls, every
criterion score fixed 0.5 — the rule follows from pairwise needing pairs and batch-relative
percentile normalization (spec 3.4.3 N=1 → 0.5); pointwise (spec 3.4.4) is an absolute 0–5
scale and scores a single record normally via one real call per criterion.
`item.scores` keys: every criterion key + `"__aggregate__"`. Trace: `quality.judgment` (one per
judgment, per judge, per order), `quality.pointwise`, `quality.bt_fit` (per criterion per batch),
`quality.gate` (per gated record). `judgment_reasons` "auto" = on iff `trace.enabled` and
`"quality" in trace.channels`.

### 7.4 M5 — `labelkit/annotate.py`

```python
@dataclass(frozen=True)                            # [FROZEN HERE]
class RepairContext:
    previous_output: Mapping                       # last annotation object
    critiques_text: str                            # rendered lines "aspect: opinion"
                                                   # (multi-judge: "judge_name/aspect: opinion")


def build_annotate_prompt(record: Record, cfg: ResolvedConfig, schema_text: str,
                          repair: RepairContext | None = None,
                          temperature: float | None = None) -> PromptBundle:
    """Deterministic template assembly per §10.1. schema_text = SchemaEngine.user_schema_text.
    repair != None appends the repair suffix (§10.5). [FROZEN HERE]"""


async def annotate_record(record: Record, ctx: RunContext,
                          repair: RepairContext | None = None) -> Annotation:
    """One record's full annotation path incl. self-consistency (skipped when repair != None:
    repair re-annotation is always a single call at profile-default temperature [FROZEN HERE]).
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError. This is the hook M7
    uses for verify.policy='repair'. [FROZEN HERE]"""


class AnnotateStage(Stage):
    name = "annotate"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
```

Normative behavior: per active item, `item.annotation = await annotate_record(...)`; on
`SchemaViolation` → `status="failed"`, kind `schema_violation`; provider exhausted → `failed`,
kind `provider_retryable_exhausted`; UI image decode error → `failed`, kind `image_decode_error`.
Self-consistency (`self_consistency = n ≥ 3`): n independent samples at `sc_temperature`, each
through the full M8 guarantee; field-level vote: enum/boolean/integer properties → per-field mode;
all other fields (string free text, arrays, numbers, nested objects) taken wholesale from the
first sample matching the modal voted-field combination; no such sample / no modal combination →
take sample #1 entirely and count `report.annotate.sc_disagreements`; a failed sample abstains
(denominator stays n); all n fail → `failed`. `Annotation.attempts` = sum of attempts over the
SUCCESSFUL samples (a failed sample aborts via SchemaViolation, which carries no attempts/usage
through `complete_validated` — its attempts are unrecoverable by design); `Annotation.usage`
likewise sums successful samples only; `Annotation.sc = {"n": n, "agreement_ratio": matches/n}`. Trace: `annotate.done` with
payload `{attempts[, sc]}`. Concurrency: records within the stage run concurrently via
`asyncio.gather` (bounded by the profile semaphore in M9).

### 7.5 M6 — `labelkit/generate.py`

```python
class GenerateStage(Stage):
    name = "generate"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]:
        """PROCESS MODE. Returns the sub-batch of NEW PipelineItems (input batch untouched).
        A generation call that is invalid after M8 repair or exhausts retries is voided (bucket
        `calls` counted, `produced` 0); no failed records are created; seed records unaffected."""

    async def generate_all(self, ctx: RunContext) -> list[Record]:
        """GENERATE_ONLY MODE entry (called once by M10 before batching; ctx.batch_no == 0,
        ctx.rng == Random(f"{seed}:0:generate")). Executes all calls per the 3.6.2 count
        formulas; --limit truncates to the first ceil(limit / num_per_call) calls in pre-drawn
        order and then to limit records. [FROZEN HERE]"""
```

Normative behavior (3.6.2): seeds — process: batch items with `status=="active"` and aggregate ≥
`seed_min_score` (default `quality.threshold`, else the batch median aggregate); generate_only:
`seed_examples` strings, or seedless. Call count C = `ceil(len(seeds) * num_per_record /
num_per_call)` (seed pool same formula) / `ceil(standalone_count / num_per_call)` (seedless).
Before any concurrency, pre-draw the full `(llm, style)` assignment for call indexes `0..C-1`
with `ctx.rng`: round_robin → `llms[i % len(llms)]`; weighted → `ctx.rng.choices` per index;
style (if any) → uniform `ctx.rng.choice` per index; then per call sample
`min(seeds_per_call, len(seeds))` seeds without replacement via `ctx.rng` — all draws happen in
call-index order before dispatch so results are schedule-independent. Prompt per §10.4; output
`{"samples": [...]}` validated by M8 (`SAMPLES_SCHEMA(num_per_call)`); temperature =
`generate.temperature`. New records: `raw = {input.text_field: sample}`, id per M2 rule,
`ref = RecordRef(source_file="", line_no=None, pair_index=None, generated_from=<seed ids tuple —
process mode> | () <generate_only>, generator={"llm": name, "style": style_name_or_None})`.
Bucket stats to metrics: key `f"{llm}×{style or 'None'}"` **[FROZEN HERE: bucket key format
`<llm>×<style>` with literal `×`; style absent → the string `null` in report** — see §9.3].

### 7.6 M7 — `labelkit/verify.py`

```python
class VerifyStage(Stage):
    name = "verify"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
```

Normative behavior (3.7): per active item with an annotation, judge prompt per §10.3, output
validated against `VERDICT_SCHEMA` (§10.7). Multi-judge: independent identical prompts, verdict
by majority, all critiques merged with `"judge"` field added, one `verify.verdict` trace event
per judge per round. Policy drop: fail → `status="dropped_verify"`. Policy repair: on fail and
rounds used < `max_repair_rounds`: build `RepairContext(previous_output=item.annotation.output,
critiques_text=<critiques of the judges that voted fail, one per line, "aspect: opinion",
multi-judge prefixed with judge name>)`, call `annotate_record(record, ctx, repair)`; new
annotation replaces `item.annotation`; re-verify; still fail at budget → drop as above.
`VerificationResult.rounds` counts review rounds incl. the first; `critiques` accumulate over
rounds in order. Verify errors on a record (provider exhausted etc.) → `failed` per stage
contract.

### 7.7 M8 — `labelkit/schema_engine.py`

```python
class SchemaEngine:
    def __init__(self, user_schema: dict, llm: LLMClient, cfg: OutputConfig,
                 metrics: MetricsSink | None = None): ...        # metrics [FROZEN HERE]

    @property
    def user_schema_text(self) -> str:
        """Canonical user-schema text injected into prompts:
        json.dumps(user_schema, ensure_ascii=False, separators=(", ", ": ")) — single line.
        [FROZEN HERE]"""

    async def complete_validated(self, profile: str, prompt: PromptBundle,
                                 schema: dict | None = None, *,
                                 record_ids: tuple[str, ...] = (),
                                 batch_no: int = 0,
                                 record: Mapping | None = None) -> tuple[dict, Usage, int, str]:
        """schema=None → user schema; internal schemas (judgment/pointwise/verdict/samples)
        passed in by stages. Runs L0→L1→L2[→L2.5]→L3 (spec 3.8.2). ``record`` (v1.5,
        additive kwarg) is the raw input mapping handed to the output.validator hook
        at L2.5 — user-schema calls only; callback violations are rendered
        "(validator) <msg>", join the L3 repair prompt, and share the repair budget;
        exhaustion with ONLY callback violations left raises
        SchemaViolation(callback_only=True) → record kind callback_violation. Success: returns
        (validated_obj, total_usage, attempts, model) where attempts = 1 + L3 repair calls
        and total_usage sums the first call + repairs. Failure: raises SchemaViolation.
        Counts resolved_at buckets ONLY when schema is None (user-schema annotate calls,
        spec §6.4); emits `schema.repair` trace events (any non-clean resolution) with the
        given record_ids/batch_no. Extra kwargs and tuple return are [FROZEN HERE] (spec
        gives `-> dict`; callers need usage/attempts/model to build Annotation)."""

    def validate_only(self, obj: dict, schema: dict | None = None) -> list[str]:
        """Full-violation list (Draft202012Validator.iter_errors), rendered
        '<json-pointer>: <message>'. Empty list = valid. Used by M1 (few-shot outputs) and
        M11 (pre-write final check)."""

    @property
    def stats(self) -> dict:
        """{"l0_or_clean": int, "l1": int, "l3_1": int, "l3_2": int, "rejected": int}
        — user-schema calls only. [FROZEN HERE]"""
```

Layer definitions (normative, spec 3.8.2): **L0** — if the profile has
`supports_structured_output`, pass `schema` to `LLMClient.complete(response_schema=...)`;
validation still always runs. **L1** — pure function, in order: strip Markdown code fences → take
the first balanced-braces substring → `json_repair.loads()`; expose it as
`def deterministic_repair(text: str) -> dict | None` (module-level, unit-testable)
**[FROZEN HERE]**. **L2** — `Draft202012Validator.iter_errors()`, all violations collected.
**L3** — repair prompt per §10.6 as a single user message, profile = `cfg.repair_llm or
calling profile`, at most `cfg.max_repair_attempts` rounds, each repair output re-runs L1→L2;
exhausted → `SchemaViolation(errors, raw_last_output)`. Bucketing: clean L2 pass on first
response (whether L0 was active or L1 trivially parsed with no fence/repair needed) →
`l0_or_clean`; L1 had to fix something and L2 then passed → `l1`; passed after repair round 1/2 →
`l3_1`/`l3_2`; exhausted → `rejected`. Internal schema constants (module-level in
`schema_engine.py`, imported by stages) — exact JSON in §10.7:

```python
def judgment_schema(criteria_keys: list[str], with_reason: bool) -> dict: ...
def pointwise_schema(criterion_key: str) -> dict: ...
VERDICT_SCHEMA: dict
def samples_schema(num_per_call: int) -> dict: ...
```

### 7.8 M9 — `labelkit/llm_client.py`

```python
@dataclass(frozen=True)
class Part:
    kind: Literal["text", "image"]
    text: str | None = None
    image: ImageRef | None = None


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    parts: tuple[Part, ...]


@dataclass(frozen=True)
class PromptBundle:
    messages: tuple[Message, ...]
    temperature: float | None = None               # None = profile default


@dataclass(frozen=True)
class LLMResponse:
    text: str                                      # raw text payload (openai_compatible)
    structured: dict | None                        # anthropic tool_choice native payload, else None
    usage: Usage
    model: str
    latency_ms: int


@dataclass                                          # mutable per-profile accumulator [FROZEN HERE]
class ProfileUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    est_cost_usd: float | None = None              # only when prices configured


@dataclass(frozen=True)                             # [FROZEN HERE]
class ProbeResult:
    profile: str
    ok: bool
    model: str
    latency_ms: int
    error: str | None = None


class LLMClient:
    def __init__(self, llm_profiles: Mapping[str, LLMProfile],
                 embedding_profiles: Mapping[str, EmbeddingProfile],
                 metrics: MetricsSink | None = None): ...        # [FROZEN HERE: split dicts + metrics]

    async def complete(self, profile: str, prompt: PromptBundle,
                       response_schema: dict | None = None) -> LLMResponse:
        """response_schema becomes L0 params only if the profile declares
        supports_structured_output, else ignored. Raises ProviderRetryableError (retries
        exhausted) / ProviderFatalError / CircuitBreakerTripped (fail-fast once the breaker
        is open)."""

    async def embed(self, profile: str, texts: list[str]) -> list[list[float]]:
        """v1.2. profile must be an [embedding.*] name — [llm.*] names rejected (ValueError).
        openai_compatible only: POST {base_url}/embeddings, body {"model", "input"}; response
        data[*].embedding aligned with input order. dims configured → per-vector check,
        mismatch raises ProviderFatalError. Usage metered under the embedding profile name;
        one llm.call trace event per call with payload operation="embedding". Retry/limit
        rules identical to complete()."""

    async def probe(self, profile: str) -> ProbeResult:
        """validate --probe: minimal 1-token live call (llm profiles) or 1-text embed
        (embedding profiles). Never raises; failures land in ProbeResult.error."""

    @property
    def usage_by_profile(self) -> dict[str, ProfileUsage]: ...
```

Provider adaptation (normative, 3.9.3): `openai_compatible` POST `{base_url}/chat/completions`;
images `{"type":"image_url","image_url":{"url":"data:<media>;base64,<b64>"}}`; structured output
`response_format={"type":"json_schema","json_schema":{"name":"user_schema","strict":true,
"schema":<schema>}}`. `anthropic` POST `{base_url}/v1/messages` with `x-api-key` +
`anthropic-version: 2023-06-01` **[FROZEN HERE]**; images `{"type":"image","source":
{"type":"base64","media_type":...,"data":...}}`; structured output = single tool with the schema
as `input_schema` and `tool_choice={"type":"tool","name":"emit"}` **[FROZEN HERE: tool name
"emit"]**, result surfaced in `LLMResponse.structured`. Retries: retryable = network error,
timeout, HTTP 408/409/429/5xx; wait for attempt i = `random.uniform(0, retry_base_delay_s * 2**i)`
capped at 60 s (this jitter RNG is NOT seed-derived — timing only **[FROZEN HERE]**); honor
`Retry-After` on 429; at most `max_retries`; 401/403/400/404 → ProviderFatalError immediately
(401/403 additionally open the circuit breaker at once — auth-class failures never self-heal;
v1.5). Retry exhaustion feeds the breaker window too (`record_provider_result(fatal=True)`).
One `asyncio.Semaphore(max_concurrency)` per profile shared by ALL calls (incl. repairs,
verify, probe). Image bytes loaded/scaled/encoded per call and released. Metering: accumulate
usage from response; cost = `prompt_tokens/1e6*price_in + completion_tokens/1e6*price_out` when
both prices set. Breaker interplay: every ProviderFatalError → `metrics.record_provider_result
(fatal=True)` — with `hard=True` when status is 401/403 (immediate open, v1.5); retry
exhaustion also records `fatal=True`; any success → `record_provider_result(fatal=False)`; when
`metrics.circuit_broken`, `complete`/`embed` raise `CircuitBreakerTripped` at entry. Trace:
`llm.call` after every call (incl. failures) with the §8.2 payload; API keys never enter any log
path.

### 7.9 M10 — `labelkit/orchestrator.py`

```python
@dataclass(frozen=True)                            # [FROZEN HERE]
class RunSummary:
    counts: Mapping                                # same keys as report.json "counts" (§9.3)
    interrupted: bool
    exit_code: int                                 # 4 (circuit break) | 1 (cfg.strict and
                                                   # rejects > 0) | 0 — computed by M10 so
                                                   # report.run.exit_code records the actual
                                                   # exit code (spec §6.4); report-write
                                                   # failure (also exit 1) is decided later
                                                   # by the CLI and is the only exit-1 cause
                                                   # not representable in the report
    wall_s: float
    output_lines: int
    rejects_lines: int


class Orchestrator:
    def __init__(self, cfg: ResolvedConfig, stages: list[Stage],
                 ingestor: Ingestor | None, emitter: Emitter, llm: LLMClient,
                 schema_engine: SchemaEngine, metrics: MetricsSink,
                 run_id: str, run_started_at: datetime): ...
        # spec 3.10.3 lists (cfg, stages, ingestor, emitter, llm); the extra parameters are
        # [FROZEN HERE] — schema_engine/metrics are needed to build RunContext; run_id/
        # run_started_at feed report assembly and run-level events (NOT RunContext, spec 3.12.3).

    async def run(self) -> RunSummary: ...
```

Normative behavior: split `ingestor.records()` into batches of `run.batch_size` (`--limit`
truncates the stream to the first N records); wrap into `PipelineItem`s; per batch, per enabled
stage in order dedup → quality → generate → annotate → verify: build a fresh `RunContext` (rng
derived per §5) and `await stage.run(batch, ctx)`; `generate.run`'s return value is enqueued as
new batch(es) (split at `batch_size`, consecutive `batch_no`, no generate stage); after stages,
`emitter.emit_batch(batch, batch_no)`, then `metrics.flush()` (trace flush follows output flush),
then drop the batch. Emit events `batch.start`/`batch.end` (stage="run"). generate_only: no
ingestor; call `GenerateStage.generate_all(ctx0)` first, batch the records, run the reduced
chain. Stage timing: wall-clock per stage accumulated into `metrics` for `report.timing`
(`metrics.add_stage_time(stage_name, seconds)` **[FROZEN HERE]**). Circuit breaker: catch
`CircuitBreakerTripped` escaping a stage → cancel remaining work, finalize (report written,
`.part` NOT renamed **[FROZEN HERE]**), `RunSummary.exit_code=4`. SIGINT/SIGTERM: stop taking new
batches, wait current batch ≤ 30 s then cancel, finalize normally (rename happens; report
`interrupted=true` **[FROZEN HERE]**). Tail batch processed as-is. Report assembly is owned by
the orchestrator: it builds the §9.3 dict from `ingestor.report`, `metrics`, `schema_engine.stats`,
`llm.usage_by_profile` and timing, then calls `emitter.finalize(report)`; `report.run.exit_code`
= `RunSummary.exit_code` incl. the `--strict` escalation (4 on circuit break, else 1 when
`cfg.strict` and total rejects > 0, else 0) **[FROZEN HERE]**. Dry-run: after M1/M2
scan (or generate_only static call-count formula), print cost/call estimate to stderr and exit 0
without constructing LLM calls. Dry-run writes NO main output/rejects (`Emitter.open` is never
called; `finalize(report, deliver=False)`), but `report.json` is still written and, when
`trace.enabled`, the trace channel still records its `run.start`/`run.end` lifecycle events —
trace is a first-class opt-in output channel (spec 2.6) and carries no data content. The dry-run
stderr summary line reflects this: `(report and trace only)` when `trace.enabled`, else
`(report only)`.

### 7.10 M11 — `labelkit/emitter.py`

```python
@dataclass(frozen=True)                            # [FROZEN HERE]
class EmitResult:
    emitted: int
    rejected: int


class Emitter:                                     # signatures [FROZEN HERE]
    def __init__(self, cfg: ResolvedConfig, engine: SchemaEngine,
                 run_id: str, run_started_at: datetime): ...

    def open(self) -> None:
        """Create/truncate {output}.part (and {stem}.meta.jsonl.part when meta_mode='sidecar',
        {stem}.rejects.jsonl when rejects != 'none'). Unwritable → raise LabelKitError → exit 4."""

    def emit_batch(self, batch: list[PipelineItem], batch_no: int) -> EmitResult:
        """Distribute by status: active (and annotation present when annotate enabled) → main
        output; dropped_* / failed → rejects. Pre-write final check per line — ONLY when
        annotate.enabled (§9.1; raw data emitted by annotate-disabled runs is not expected to
        pass the user schema): engine.validate_only(user_object) — non-empty violations =
        internal bug → the item is
        diverted to rejects with kind='internal_error' (fail loudly, run continues). Appends +
        flush(). Updates stderr progress (TTY progress line; non-TTY: nothing — batch.end info
        line comes from M12/M10)."""

    def finalize(self, report: Mapping, deliver: bool = True) -> None:
        """fsync + atomic os.rename {output}.part → {output} (and sidecar) when deliver=True;
        always writes {output_stem}.report.json (cfg.dry_run diverts to {output_stem}.dryrun.report.json,
        v1.5 P2-4); prints the final stderr summary table matching
        report['counts']. deliver=False (circuit break, exit 4) leaves .part in place.
        Report write failure → CLI exit 1 (raise LabelKitError('report write failed')).
        [FROZEN HERE]"""
```

File names: main `run.output`; temp `run.output + ".part"` (same directory); sidecar
`{output_stem}.meta.jsonl` (temp `+ ".part"`); rejects `{output_stem}.rejects.jsonl` (streamed,
no .part — it is an append log like trace **[FROZEN HERE]**); report `{output_stem}.report.json`.
`output_stem` = output path minus final suffix. Line formats: §9.

### 7.11 M12 — `labelkit/obslog.py`

```python
@dataclass(frozen=True)
class TraceEvent:
    ts: str                        # ISO8601 milliseconds with timezone offset
    run_id: str                    # secrets.token_hex(6) — 12 hex chars per run
    batch_no: int                  # 0 for run-level events
    stage: str                     # emitting stage name; run.*/batch.* use "run"
    ev: str                        # event name (§8.1)
    record_ids: tuple[str, ...]    # 0/1/2 record ids
    payload: Mapping               # per-event fields (§8.1), redacted per trace.content (§8.3)


class EventLog:
    def __init__(self, cfg: TraceConfig, run_id: str): ...       # [FROZEN HERE]
    def emit(self, ev: TraceEvent) -> None:
        """Line-buffered JSONL write. No-op when the channel is disabled, filtered out, or
        closed after a write failure (callers never check). Channel = ev name prefix before
        the first '.', EXCEPT ev == "error", whose channel is the TraceEvent.stage field
        (spec 7.2: error 事件按产生它的 stage 归属通道); 'run'/'batch' prefixes bypass the
        trace.channels filter. First OSError:
        warn once on stderr, close the channel, count every subsequent event as dropped."""
    def flush(self) -> None: ...
    def close(self) -> None: ...
    dropped_events: int
    events_written: int
    closed: bool                   # read-only: channel shut by a write failure; M10 reads it
                                   # to pre-count the terminal run.end in report.trace (§9.3)


class MetricsSink:
    """Holds the EventLog + run counters. All stages emit through RunContext.metrics."""
    def __init__(self, cfg: ResolvedConfig, run_id: str, event_log: EventLog): ...

    def event(self, ev: str, *, stage: str, batch_no: int,
              record_ids: tuple[str, ...] = (), payload: Mapping | None = None) -> None:
        """Builds the TraceEvent (ts=now local ISO8601 ms, run_id) and forwards to EventLog;
        also mirrors to the stderr logger at the §8.1 level when one is defined. [FROZEN HERE]"""

    def count(self, key: str, n: int = 1) -> None      # counter keys listed in §9.3
    def add_stage_time(self, stage: str, seconds: float) -> None
    def record_provider_result(self, fatal: bool, *, hard: bool = False) -> None
        # hard=True (auth-class 401/403 fatals) opens the breaker IMMEDIATELY (v1.5)
    @property
    def circuit_broken(self) -> bool: ...              # fatal streak >= run.fatal_error_threshold
    def flush(self) -> None                            # forwards to EventLog.flush
    counters: dict[str, int]


def setup_logging(cfg: ResolvedConfig) -> None:
    """Installs the stderr handler on logger 'labelkit' per tool.log_format/log_level.
    text format: '{ts} {level:<5} {stage:<7} batch={batch} {msg}' (stage/batch from
    record extras, '-' when absent). jsonl format: {"ts","level","stage","batch","msg"}.
    Modules log via logging.getLogger('labelkit.<module>') with extra={'stage':..., 'batch':...}.
    [FROZEN HERE: extras mechanism]"""
```

Behavior (3.12.4): trace file first line is always the `run.start` header event carrying
`trace_schema_version: 1` (only there); existing `trace.path` truncated with one stderr warn; no
atomic rename for trace (flushed prefix is valid); flush coupled to M11 batch flush via
orchestrator calling `metrics.flush()` after `emit_batch`. API keys never reach either channel.

Event-name constants (module level, exact strings): `EV_RUN_START = "run.start"`,
`EV_RUN_END = "run.end"`, `EV_BATCH_START = "batch.start"`, `EV_BATCH_END = "batch.end"`,
`EV_INGEST_BAD_LINE = "ingest.bad_line"`, `EV_INGEST_MISSING_PAIR = "ingest.missing_pair"`,
`EV_INGEST_INDEX_CONFLICT = "ingest.index_conflict"`, `EV_DEDUP_DUPLICATE = "dedup.duplicate"`,
`EV_QUALITY_JUDGMENT = "quality.judgment"`, `EV_QUALITY_POINTWISE = "quality.pointwise"`,
`EV_QUALITY_BT_FIT = "quality.bt_fit"`, `EV_QUALITY_GATE = "quality.gate"`,
`EV_ANNOTATE_DONE = "annotate.done"`, `EV_VERIFY_VERDICT = "verify.verdict"`,
`EV_SCHEMA_REPAIR = "schema.repair"`, `EV_LLM_CALL = "llm.call"`, `EV_ERROR = "error"`.

### 7.12 CLI — `labelkit/cli.py`

```
labelkit run      --config <config.toml> --project <project.toml>
                  [--input PATH] [--output PATH] [--limit N] [--dry-run] [--strict]
                  [--log-level debug|info|warn|error]
labelkit validate --config <config.toml> --project <project.toml> [--probe]
labelkit rubric   [--show default:text|default:ui]
```

```python
def main(argv: list[str] | None = None) -> int:    # entry point (pyproject console script)
```

Wiring order for `run` (owned by cli.py): parse args → `config.load()` → `setup_logging` →
`run_id = secrets.token_hex(6)`, `run_started_at = datetime.now().astimezone()` →
`EventLog` + `MetricsSink` → `LLMClient` → `SchemaEngine` → stages per switches (`DedupIndex`
constructed here, passed to `DedupStage`) → `Ingestor` (process mode) → `Emitter` →
`Orchestrator` → `asyncio.run(orch.run())` → exit code: `ConfigError`→2, `InputError`→3,
fatal (`RunSummary.exit_code==4` / unwritable output / auth failure)→4, `--strict` and
rejects>0 → 1 (already folded into `RunSummary.exit_code` by M10, §7.9), report write
failure → 1, else 0. `validate`: `config.load()` only (+`--probe`:
`LLMClient.probe` on every referenced profile, print results; any probe failure does not change
the exit code unless config itself is invalid **[FROZEN HERE]**). `rubric`: no flag → list
available names; `--show <name>` → print the packaged TOML verbatim.

---

## 8. Observability contract (M12 + ch.7)

### 8.1 Event catalog (stable contract, `trace_schema_version = 1`, additive-only)

| Event `ev` | Channel / stderr level | Emitted by / when | `record_ids` | payload fields |
|---|---|---|---|---|
| `run.start` | always / info | M10, after M1 passes, before first batch; trace header line | () | `tool_version`, `config_digest`, `project_digest`, `trace_schema_version` (=1, only here) |
| `run.end` | always / info | M10 after finalize; last trace line | () | `counts` (report-shaped summary), `exit_code` |
| `batch.start` | always / debug | M10 when PipelineItem[] ready | () | `size` |
| `batch.end` | always / info | M10 after batch emit + release | () | `active`, `dropped_dup`, `dropped_lowq`, `dropped_verify`, `failed`, `duration_ms` |
| `ingest.bad_line` | ingest / warn | M2 bad line skipped | () | `file`, `line_no`, `reason` |
| `ingest.missing_pair` | ingest / warn | M2 missing pair skipped | () | `index`, `present` ("tree"\|"image"), `file` |
| `ingest.index_conflict` | ingest / warn (error if policy=fail) | M2 index conflict | () | `index`, `files` (list) |
| `dedup.duplicate` | dedup / — | M3 duplicate verdict | (dup id,) | `kind`, `cluster_key`, `kept_id`, plus exactly one of `jaccard` (near_text) / `hamming` (near_image) / `cosine` (near_semantic); exact dups carry none |
| `quality.judgment` | quality / — | M4 per pairwise judgment after M8 pass | (first-sampled record, second-sampled record) — SAMPLING order, NOT the presented A/B order; the A/B mapping lives in `payload.order` (spec 7.2/7.3) | `order` ({"A": id, "B": id} presented), `model`, `judgments`[]{`criterion`, `winner` "A"\|"B"\|"tie"[, `reason`]}[, `judge`] |
| `quality.pointwise` | quality / — | M4 per record per criterion | (id,) | `criterion`, `score` (raw 0–5), `reason` |
| `quality.bt_fit` | quality / — | M4 per batch per criterion | () | `criterion`, `iterations`, `converged`, `comparisons` |
| `quality.gate` | quality / — | M4 gate decision per record (threshold set or top_ratio) | (id,) | `aggregate`, `decision` ("keep"\|"drop")[, `threshold`][, `selection`, `top_ratio`, `rank`] |
| `annotate.done` | annotate / — | M5 after M8 pass | (id,) | `attempts`[, `sc` {n, agreement_ratio}] |
| `verify.verdict` | verify / — | M7 per round (per judge when judges set) | (id,) | `verdict`, `round`, `critiques`[]{`aspect`, `opinion`}[, `judge`] |
| `schema.repair` | schema / — | M8 any non-clean resolution | (record ids if known) | `resolved_at` ("l1"\|"l3_1"\|"l3_2"\|"rejected"), `violations` (JSON-Pointer + violated keyword summary, NO data values)[, `l1_lossy`=true — v1.5, only on a suspected content-dropping L1 repair] |
| `llm.call` | llm / debug (summary always) | M9 after every call incl. failures | () | `profile`, `gen_ai.request.model`, `latency_ms`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `retries`, `status` ("ok"\|"retryable_exhausted"\|"fatal")[, `operation`="embedding"][, `gen_ai.input.messages`, `gen_ai.output.messages` — content="full" + llm channel only] |
| `error` | channel of producing stage / warn (record-level) · error (run-level) | On StageError construction | per case | `stage`, `kind` (§7.6 codes), `message`, `retryable` |

`reason` present only when `quality.judgment_reasons` is effective. `run.*`/`batch.*` bypass the
`trace.channels` filter and use `stage="run"`, `batch_no` = current batch (0 for run.*).

### 8.2 Trace line format

One JSON object per line, UTF-8, exactly the seven fields
`ts, run_id, batch_no, stage, ev, record_ids, payload` (test-asserted). `ts` ISO8601 with
milliseconds and timezone offset, e.g. `2026-07-02T09:31:04.482+08:00`.

### 8.3 `trace.content` redaction tiers

| Tier | Payload content |
|---|---|
| `"none"` | ids, enums, numbers only; NO LLM-produced free text (`reason`/`critiques`/`violations` omitted) |
| `"refs"` (default) | + LLM-produced text (reason / critiques / violations), NO input data content |
| `"excerpt"` | + `excerpt` field on `quality.judgment` / `quality.pointwise` / `annotate.done` / `verify.verdict`: `{record_id: first 200 chars}` (text: `Record.text`; UI: `UITree.serialize()` output; never images) |
| `"full"` | + `gen_ai.input.messages` / `gen_ai.output.messages` on `llm.call` (requires "llm" in channels) |

API keys appear at no tier, in no channel.

### 8.4 stderr run-log formats (spec 7.3)

```
# text (default):  {ts} {LEVEL:<5} {stage:<7} batch={n|-} {msg}
2026-07-02T09:31:04+08:00 INFO  quality batch=3 pairwise 完成 items=128 comparisons=256 judgment_failures=1
# jsonl:
{"ts":"...","level":"info","stage":"quality","batch":3,"msg":"..."}
```

stderr NEVER contains data content, prompts, or API keys. `log_format="jsonl"` disables the
progress bar (every stderr line must be `json.loads`-able). Progress display (TTY bar / non-TTY
per-batch summary) is not logging: written directly to stderr by M11/M12 without the logging
module.

---

## 9. Output contracts (ch.6)

### 9.1 Main output + `_meta` (spec §6.3)

`meta_mode="inline"`: each line = user-schema fields at top level + reserved `_meta` key;
stripping `_meta` must yield an object passing the user schema. `"sidecar"`: main line = pure
user object; `_meta` objects (wrapped as `{"_meta": {...}}` — same shape as inline value
**[FROZEN HERE]**) written line-aligned to `{output_stem}.meta.jsonl`. `"none"`: user object
only. Lines are `json.dumps(obj, ensure_ascii=False)` compact **[FROZEN HERE]**.

**Annotate disabled** (`annotate.enabled = false` — a spec-legal combination, spec 2.3.1 row 2:
"dedup + quality only, output = filtered raw data + scores") **[FROZEN HERE, see §12]**: the
emitted user object is `Record.raw` (text modality) or
`{"ui_tree": record.ui_tree.serialize(), "image_path": str(record.image.path)}` (UI modality —
same shape as the rejects `full`-tier record payload, §9.2); the pre-write `validate_only`
check is skipped (§7.10); `_meta` attaches per `meta_mode` as usual with `annotation: null`.

`_meta` structure (all keys always present; unused stage keys are `null`):

```jsonc
"_meta": {
  "id": "<record id>",
  "run": {"tool": "labelkit/1.0.0", "started_at": "<ISO8601>",
          "project_file": "<project.toml path as given>", "rubric": "<rubric name selector,
          e.g. 'default:ui' or the inline rubric's name>", "seed": <run.seed>},
  "source": {"file": "<ref.source_file>",
             // exactly one of the following two: "line_no" when ref.line_no is non-null,
             // otherwise "pair_index" with its value — generated records (both refs null)
             // therefore emit "pair_index": null, matching the spec 3.6.4 worked example
             // [FROZEN HERE, see §12]:
             "line_no": <int>, "pair_index": <int|null>,
             "generated_from": [<seed ids>],          // [] unless process-mode generated
             "fields": {<output.passthrough_fields from Record.raw>},   // {} when none
             "generator": null | {"llm": "<profile>", "style": "<name>"|null}},
  "scores": null | {"<criterion>": <float|null>, ..., "__aggregate__": <float|null>,
                    "mode": "pairwise_bt"|"pointwise", "batch_no": <int>},
  "dedup": null | {"kind": "unique"},
  "annotation": null | {"model": "<model>", "attempts": <int>
                        [, "sc": {"n": <int>, "agreement_ratio": <float>}]},
  "verification": null | {"verdict": "pass"|"fail", "rounds": <int>}
}
```

`_meta.run.rubric` = the configured selector (`"default:text"`/`"default:ui"`) or, for inline,
the rubric's `name` **[FROZEN HERE]**. A disabled stage → `null` for its key.

### 9.2 Rejects channel (spec 3.11.2)

`{output_stem}.rejects.jsonl`. `rejects="refs"` (default) — one line per rejected item, no data
content whatsoever (no passthrough fields either). Per spec 3.11.2 the refs line carries
**exactly** the five `_meta` keys `{id, source, stage, reason, errors}` (a closed enumeration:
每行仅 …) — no status-specific evidence keys. Duplicate-cluster / quality-gate / verdict
evidence is auditable via the trace events instead (`dedup.duplicate`, `quality.gate`,
`verify.verdict`, §8.1):

```jsonc
{"_meta": {
  "id": "<record id>",
  "source": {"file": ..., "line_no"/"pair_index": ... (same convention as §9.1),
             "generated_from": [...] [, "generator": {...}]},   // NO "fields"
  "stage": "<stage that rejected>",         // dedup | quality | verify | annotate | emitter ...
  "reason": "<see table>",
  "errors": [ "<pointer>: <violation>", ... ]   // always present; [] when item.errors is empty
                                                // [FROZEN HERE: [] rather than omission]
}}
```

`reason` values **[FROZEN HERE]**: `dropped_dup` → the DedupInfo kind (`"exact"`,
`"near_text"`, `"near_image"`, `"near_both"`, `"near_semantic"`); `dropped_lowq` →
`"below_threshold"` or `"top_ratio"`; `dropped_verify` → `"verify_fail"`; `failed` → the first
`StageError.kind`. `rejects="full"` adds `"record"` (text: `Record.raw`; UI:
`{"ui_tree": serialize(), "image_path": str}` **[FROZEN HERE]**) and `"raw_last_output"` (for
schema_violation). `rejects="none"`: no file.

### 9.3 `report.json` (spec §6.4)

```jsonc
{
  "run": {"tool_version": "1.0.0", "started_at": "...", "finished_at": "...",
          "interrupted": false, "exit_code": 0, "modality": "ui", "seed": 42,
          "config_digest": "sha256:...", "project_digest": "sha256:..."},
  "counts": {"scanned": 0, "ingested": 0, "bad_input": 0,
             "dropped_dup": 0, "dropped_lowq": 0, "dropped_verify": 0,
             "failed": 0, "generated": 0, "emitted": 0},
  "dedup": {"exact": 0, "near_text": 0, "near_image": 0, "near_both": 0,
            "clusters": 0, "image_decode_failures": 0
            /* + when dedup.semantic: "near_semantic": 0, "embedding_failures": 0 */},
  "quality": {"mode": "pairwise_bt", "rounds": 4, "judgment_failures": 0,
              "aggregate_histogram": {"0.0-0.1": 0, "0.1-0.2": 0, ..., "0.9-1.0": 0},  // 10 buckets
              "per_criterion_mean": {"<criterion>": 0.0, ...}},
  // run block also carries "circuit_broken": false (v1.5, always present);
// pairwise quality additionally carries "per_criterion_tie_rate" (v1.5, judged comparisons only)
  "schema_engine": {"resolved_at": {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0,
                                    "rejected": 0}},
  // optional blocks:
  // "annotate": {"sc_disagreements": 0}                       (self-consistency enabled)
  // "generate": {"buckets": {"<llm>×<style|null>": {"calls": 0, "produced": 0,
  //                                                 "survived_dedup": 0}}} (generate enabled)
  "trace": {"enabled": true, "path": "...", "events": 0, "dropped_events": 0},
  "llm_usage": {"<profile>": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                              "est_cost_usd": 0.0, "retries": 0}, ...},
  "timing": {"wall_s": 0, "per_stage_s": {"dedup": 0, "quality": 0, "annotate": 0,
                                          "verify": 0 /* enabled stages only */}}
}
```

**Counts invariant (test-asserted):**
`emitted + dropped_dup + dropped_lowq + dropped_verify + failed + bad_input = scanned + generated`.
generate_only degenerates to `emitted + dropped_* + failed = generated` (scanned = bad_input = 0).
`schema_engine.resolved_at` counts ONLY user-schema annotate calls; its sum = records entering M5.
`est_cost_usd` present only for profiles with both prices configured. Histogram bucket labels are
exactly `"0.0-0.1"` … `"0.9-1.0"` (upper bound inclusive on the last) **[FROZEN HERE]**. The
report contains no data content anywhere. `quality.mode` in the report is `"pairwise_bt"` or
`"pointwise"` (the QualityScore mode string) **[FROZEN HERE]**.

`trace.events` / `trace.dropped_events` describe the FINAL trace file, including the terminal
`run.end` event, even though `run.end` is emitted only after the report is assembled (§8.1:
run.end is the trace's last line, written after finalize, its payload carrying the report
counts). M10 accounts for the pending `run.end` at report-assembly time when `trace.enabled`:
`events` += 1 while the channel is open, `dropped_events` += 1 when a write failure already
closed it (`EventLog.closed`). Invariant: `report.trace.events` == number of lines in the trace
file (barring a write failure on the `run.end` line itself).

MetricsSink counter keys **[FROZEN HERE]**, mapped 1:1 onto the above: `counts.*`
(`scanned/ingested/bad_input/dropped_dup/dropped_lowq/dropped_verify/failed/generated/emitted`),
`dedup.exact/near_text/near_image/near_both/near_semantic/clusters/image_decode_failures/
embedding_failures`, `quality.judgment_failures`, `annotate.sc_disagreements`,
`generate.buckets.<key>.calls/produced/survived_dedup` (+ `.rejected_by_validator` when
`generate.sample_validator` is set, v1.5).

Counter OWNERSHIP (normative): `counts.*` keys are incremented ONLY by M10 (orchestrator),
derived from batch tallies / EmitResult — stages must never touch them (double-count).
Stage-scoped keys are incremented only by their stage: `dedup.*` by M3, `quality.judgment_failures`
by M4, `annotate.sc_disagreements` by M5, `generate.buckets.*` by M6 (`survived_dedup` = records
surviving M6's own MinHash novelty filter against seeds + siblings; M3 still dedups generated
records on re-flow).

### 9.4 Atomic delivery

Main output (and sidecar) is appended to `<name>.part` with per-batch flush; finalize = fsync +
`os.rename` to the target name. At any instant the directory holds either the `.part` or the
final file, never a half-written final file. Consumers treat the appearance of the final name as
the completion signal. Exit-4 (circuit break) and unhandled crashes leave `.part`; graceful
SIGINT finalize renames.

---

## 10. Prompt templates (verbatim, normative)

Placeholders in `{...}` are substituted; everything else is emitted byte-for-byte. All templates
are deterministic string assembly — no "smart" rewriting. JSON objects injected into prompts use
`json.dumps(obj, ensure_ascii=False)` **[FROZEN HERE]**.

### 10.1 M5 annotation prompt (spec 3.5.2)

```
system:
  {annotate.instruction}
  输出必须是符合以下 JSON Schema 的单个 JSON 对象，不输出任何其他内容：
  {user_schema_json}                       ← SchemaEngine.user_schema_text
user (one message per few-shot example, in order):
  [示例输入] {example.input}
  [示例输出] {json.dumps(example.output, ensure_ascii=False)}
user (current record):
  text modality — single text part:
      [待标注数据] {record.text}
  UI modality — three parts in one user message:
      text part:  [屏幕截图]
      image part: record.image  (encoded by M9 at call time)
      text part:  [UI 控件树]
                  {record.ui_tree.serialize(max_chars=input.ui_tree_max_chars)}
```

### 10.2 M4 pairwise judging prompt (spec 3.4.3 / worked example 3.4.6 ③)

```
system:
  你将对两条记录进行成对质量比较。准则如下：
  - {criterion.key}: {criterion.description}
    {criterion.pairwise_prompt}
  （↑ one two-line block per criterion, in rubric order; criteria_per_call="single" → exactly
     one block and one call per criterion）
  对每条准则给出裁决。输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie", "reason": <一句话理由>}]}
user:
  [记录 A] {content of the record presented as A}
  [记录 B] {content of the record presented as B}
```

When `judgment_reasons` is not effective, the structure line is instead exactly:
`{"judgments": [{"criterion": <准则 key>, "winner": "A"|"B"|"tie"}]}` **[FROZEN HERE]**.
UI modality: the user message replaces each `[记录 X] ...` line with three parts —
text `[记录 A 屏幕截图]`, the image part, text `[记录 A UI 控件树]\n{serialize(max_chars=
input.ui_tree_max_chars)}` (same for B) **[FROZEN HERE labels]**. Record content for text
modality = `record.text`.

### 10.3 M4 pointwise prompt (spec 3.4.4 / 3.4.6 ⑦) — one call per record per criterion

```
system:
  按以下 0–5 加性量表为记录的 {criterion.key}（{label}）打分，先给两句理由再给整数分：
  {pointwise_levels[0]}
  {pointwise_levels[1]}
  {pointwise_levels[2]}
  {pointwise_levels[3]}
  {pointwise_levels[4]}
  {pointwise_levels[5]}
  输出 JSON：{"scores": [{"criterion": <准则 key>, "reason": <两句理由>, "score": 0..5}]}
user:
  [记录内容] {record content — text: record.text; UI: image + tree parts as in 10.2}
```

`{label}` = `criterion.description` up to (excluding) its first `：`, or the whole description if
it contains no `：` **[FROZEN HERE]** (matches the spec's worked example
`educational_value（教育/训练价值）`).

### 10.4 M6 generation prompt (spec 3.6.2; structure fixed, wording frozen here)

```
system:
  {generate.instruction}
  [风格要求] {style.prompt}                 ← only when a style was drawn for this call
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"samples": [<新样本文本>, ...]}（恰 {num_per_call} 条）
user:
  [种子示例 1] {seed_1_text}
  [种子示例 2] {seed_2_text}
  ...                                       ← omitted entirely in the seedless form
  请生成 {num_per_call} 条全新样本。
```

Seed text = `record.text` (process mode) / the seed string (seed-pool form). The system schema
sentence, `[种子示例 N]` labels and the final user line are **[FROZEN HERE]** (spec fixes only
the `[风格要求]` prefix and the `{"samples": [...]}` shape).

### 10.5 M7 verify prompt + repair feedback (spec 3.7.2 / 3.7.3, verbatim)

```
system:
  你是标注质量审核员。给定任务指令、原始数据与标注结果，独立判断标注是否合格。
  评审维度: ① 是否遵循任务指令 ② 与原始数据的事实一致性 ③ 字段语义是否正确填写
  {verify.extra_criteria}                   ← line omitted when empty
  先逐维度给出简短意见，再给结论。
user:
  [任务指令] {annotate.instruction}
  [原始数据] {record content — text: record.text; UI: image + tree parts as in 10.2}
  [标注结果] {json.dumps(annotation.output, ensure_ascii=False)}
```

Repair suffix appended (as additional text at the end of the final user message) to the §10.1
annotation prompt when re-annotating (`RepairContext`):

```
[上一版标注] {json.dumps(previous_output, ensure_ascii=False)}
[审核意见] {critiques_text}                 ← one per line: "aspect: opinion";
                                              multi-judge: "judge_name/aspect: opinion" [FROZEN HERE]
请修正后重新输出
```

### 10.6 M8 L3 repair prompt (spec 3.8.2 / 3.8.4, verbatim) — single user message

```
[原始输出]
{raw LLM output, unmodified, in full}

[违规清单]
{numbered violations, 1-based, one per line:
 "N. {json_pointer}: {violation description incl. expected vs actual}"}

只输出修正后的 JSON。
```

### 10.7 Internal schemas (M8 module constants; exact JSON)

```python
def judgment_schema(criteria_keys, with_reason):
    item_props = {"criterion": {"type": "string", "enum": list(criteria_keys)},
                  "winner": {"type": "string", "enum": ["A", "B", "tie"]}}
    required = ["criterion", "winner"]
    if with_reason:
        item_props["reason"] = {"type": "string"}
        required = ["criterion", "winner", "reason"]
    return {"type": "object",
            "properties": {"judgments": {"type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": required, "additionalProperties": False},
                "minItems": len(criteria_keys), "maxItems": len(criteria_keys)}},
            "required": ["judgments"], "additionalProperties": False}

def pointwise_schema(criterion_key):
    return {"type": "object",
            "properties": {"scores": {"type": "array",
                "items": {"type": "object",
                          "properties": {"criterion": {"type": "string", "enum": [criterion_key]},
                                         "reason": {"type": "string"},
                                         "score": {"type": "integer", "minimum": 0, "maximum": 5}},
                          "required": ["criterion", "reason", "score"],
                          "additionalProperties": False},
                "minItems": 1, "maxItems": 1}},
            "required": ["scores"], "additionalProperties": False}

VERDICT_SCHEMA = {          # critiques BEFORE verdict: reason-then-conclusion (spec 3.8.3 note)
    "type": "object",
    "properties": {"critiques": {"type": "array",
                       "items": {"type": "object",
                                 "properties": {"aspect": {"type": "string"},
                                                "opinion": {"type": "string"}},
                                 "required": ["aspect", "opinion"],
                                 "additionalProperties": False}},
                   "verdict": {"type": "string", "enum": ["pass", "fail"]}},
    "required": ["critiques", "verdict"], "additionalProperties": False}

def samples_schema(num_per_call):
    return {"type": "object",
            "properties": {"samples": {"type": "array", "items": {"type": "string"},
                                       "minItems": num_per_call, "maxItems": num_per_call}},
            "required": ["samples"], "additionalProperties": False}
```

All four are **[FROZEN HERE]** (spec fixes the shapes, not the exact schema JSON).

---

## 11. Cross-cutting conventions (binding)

1. **Async everywhere LLM is involved.** `Stage.run`, `complete_validated`, `complete`, `embed`,
   `probe`, `Orchestrator.run` are `async def`. Record-level concurrency inside a stage via
   `asyncio.gather`; stages are serial within a batch (barrier); batches are serial.
2. **Stages never remove items** — status flips only; `generate` returns a new list instead.
3. **Single-record failures never escape**: `item.errors.append(StageError(...))` +
   `status="failed"` + `error` trace event; the run continues. Record-level isolation is absolute.
4. **Determinism.** All sampling RNGs derive from `run.seed` exactly as §5; temperature default
   0.0; generate pre-draws its (llm, style, seeds) plan in call-index order before dispatch;
   top_ratio ties broken by record id ascending; same input + same seed ⇒ byte-identical pairing
   plan and selection decisions. Retry jitter is exempt (timing only).
5. **No data persistence**: no temp files beyond the declared output channels (`.part` files are
   part of output delivery); no caches, checkpoints, or cross-run state; only DedupIndex,
   MetricsSink counters and M9 usage survive across batches, all content-free.
6. **Atomic delivery**: main output/sidecar via `.part` + fsync + rename (§9.4).
7. **Privacy**: data goes only to configured endpoints; API keys only via env → memory
   (`repr=False` fields), never in logs, traces, reports, or exceptions; stderr never carries
   data content or prompts; trace content is tiered per §8.3; reports contain counts only.
8. **LLM output is untrusted**: every LLM-produced object (annotations, judgments, verdicts,
   samples, repairs) passes M8 L2 validation before use; M11 re-validates before writing.
9. **Memory**: image bytes loaded per request and released; batch intermediates dropped after
   emit; ≤500k records design target.
10. **Log-write failures never interrupt the run** (warn once, close channel, count drops).

---

## 12. Registry of decisions frozen by this document

Spec-silent or spec-ambiguous points, resolved here (do not re-litigate in code review):

1. `UITree.serialize` indentation = **two spaces per depth** (ch.4 formula says `" "*depth`, but
   all worked examples show two; examples win). Truncation marker line `…(truncated N nodes)`;
   quantization = floor division, quantized values serialized directly.
2. `QualityScore.score` is `float | None` to represent the unscored (`on_unscored`) state; an
   unscored record dropped via `on_unscored="drop"` gets `status="dropped_lowq"`.
3. `Annotation.sc` field added to carry self-consistency stats to `_meta`; repair re-annotation
   (M7 loop) skips self-consistency and uses profile-default temperature.
4. `Usage.__add__` plus `Usage.__radd__` (returns `self` when the left operand is `0`, else
   `NotImplemented`) so plain `sum(usage_list)` works; per-profile accumulator
   `ProfileUsage{calls, prompt_tokens, completion_tokens, retries, est_cost_usd}`.
5. `RunContext` is exactly the spec's six fields (cfg, llm, schema_engine, rng, batch_no,
   metrics — spec 3.10.3); spec 3.12.3 forbids changing its signature, so `run_id` /
   `run_started_at` travel via the Orchestrator/Emitter/MetricsSink constructors instead.
   One RunContext per (batch, stage) invocation.
6. `LLMProfile`/`EmbeddingProfile` carry `name` and resolved `api_key` (`repr=False`);
   `EmbeddingProfile.retry_base_delay_s` defaults 1.0 (spec table omits it but mandates the same
   retry mechanism). Config digests = sha256 of raw file bytes, rendered `"sha256:<hex>"`.
7. `SchemaEngine.complete_validated` returns `(dict, Usage, attempts, model)` and accepts
   `record_ids`/`batch_no` kwargs for trace attribution; constructor takes optional `metrics`;
   `user_schema_text` = single-line `json.dumps(..., ensure_ascii=False, separators=(", ", ": "))`;
   L1 exposed as module-level `deterministic_repair()`; internal schema JSONs of §10.7.
8. `LLMClient.__init__` takes split `llm_profiles`/`embedding_profiles` dicts + `metrics`;
   Anthropic structured output uses a tool named `"emit"` and header
   `anthropic-version: 2023-06-01`; retry jitter RNG is not seed-derived;
   `CircuitBreakerTripped` exception + fail-fast at call entry once the breaker is open.
9. `DedupIndex(cfg, modality)` constructor, `reset()`, `last_similarity`, `semantic_probe`/
   `add_vector` (semantic embedding is one `embed()` call per participating record — that part
   is spec 3.3.3, not frozen here); image decode
   failure leaves the record active (no StageError) and only counts `image_decode_failures`.
10. `Ingestor.metrics` public attribute for trace wiring; pairing regexes with case-insensitive
    extensions; `IngestPlan`/`IngestReport` shapes.
11. M5/M7 repair hook: `annotate.build_annotate_prompt` / `annotate_record` / `RepairContext`;
    critiques rendered `"aspect: opinion"` (multi-judge `"judge/aspect: opinion"`).
12. Generation prompt wording beyond spec-fixed fragments (`[种子示例 N]`,
    `请生成 {n} 条全新样本。`, schema sentence); `generate_all()` as the generate_only entry;
    `--limit` truncation of pre-drawn calls; bucket key `"<llm>×<style|null>"`.
13. Pairwise judging structure line without `reason` when reasons are off; UI part labels
    `[记录 A 屏幕截图]` / `[记录 A UI 控件树]`; pointwise `{label}` = description up to the
    first `：`.
14. Emitter API (`open`/`emit_batch`/`finalize(report, deliver)`); rejects file streamed without
    `.part`; rejects `reason` vocabulary; refs lines carry exactly the five spec keys
    {id, source, stage, reason, errors} (spec 3.11.2 closed enumeration; `errors` always
    present, `[]` when none); `full`-tier record
    payload shape for UI; sidecar lines wrapped as `{"_meta": {...}}`; compact
    `ensure_ascii=False` JSON everywhere in outputs.
15. Finalize semantics: SIGINT → rename + `interrupted=true`; circuit break (exit 4) → report
    written, `.part` NOT renamed.
16. `_meta.run.rubric` = configured selector string (inline → rubric name); disabled stages →
    `null` in `_meta`; histogram bucket labels `"0.0-0.1"`…`"0.9-1.0"`; report `quality.mode`
    uses the `pairwise_bt`/`pointwise` strings; MetricsSink counter-key vocabulary.
17. Orchestrator extra constructor params (`schema_engine`, `metrics`, `run_id`,
    `run_started_at`), `RunSummary` shape, report assembly owned by M10 —
    `RunSummary.exit_code` / `report.run.exit_code` fold in the `--strict` escalation
    (1 when cfg.strict and rejects > 0; report-write failure is the only exit-1 cause not
    representable in the report),
    `add_stage_time` for `timing.per_stage_s`, sub-batches enqueued with consecutive batch
    numbers after the parent batch.
18. `MetricsSink.event(...)` builder signature; `EventLog(cfg, run_id)`; stderr formatter via
    logging `extra={'stage','batch'}`; event-name constants list.
19. CLI: `validate --probe` failures print results without changing the exit code; `rubric`
    without `--show` lists names; exception→exit-code mapping lives only in `cli.py`.
20. Generated records' `_meta.source` emits `"pair_index": null` (never `line_no`), matching
    the spec 3.6.4 worked example; ingested records emit whichever of line_no/pair_index is
    non-null (§9.1 rule reproduces both spec examples).
21. Annotate-disabled runs (spec 2.3.1 row 2): main-output user object = `Record.raw` (text) /
    `{"ui_tree": serialize(), "image_path": str}` (UI); emitter pre-write `validate_only`
    check skipped in that configuration (§9.1/§7.10).
22. `schema_version` is validated (= 1 in both files, §6.3 rule 1) but deliberately not
    mirrored into the config dataclasses — a recorded deviation from spec 3.1.2's
    "typed mirror of ALL keys" wording (§6.1).
23. §6.3 rule 13 additionally requires every user-schema `$ref` to resolve locally
    against the schema document (walk of schema positions, skipping data positions
    `const`/`enum`/`default`/`examples`, with `$id` base-URI tracking; resolution via
    `referencing` with subresource crawl). Spec 3.1.5's rule list stops at
    `check_schema`, but the tool never retrieves external schema resources at runtime,
    so an unresolvable `$ref` (remote URI, relative path, or dangling local pointer)
    would otherwise fail every record inside M8 — violating M1's contract
    不存在运行期配置错误 (spec 3.1). The rule-15 few-shot validation keeps a defensive
    try/except as backstop for resolution failures the walk cannot see (e.g.
    `$dynamicRef`), aggregating them into the same ConfigError instead of crashing.
24. Dedup level ③ pHash matching is a **linear scan** over all kept hashes, NOT the
    16-bit-prefix bucketing spec 3.3.3 mentions as an acceleration: exact-prefix
    bucketing is unsound for Hamming ≤ 8 (two hashes within distance 8 can differ
    inside the prefix), and the same spec row declares linear-scan latency acceptable
    at the ≤ 500k scale target. Correctness wins over the suggested optimization.

— End of contract. —

# LabelKit — Cross-Module Interface Contract (CONTRACTS.md)

**Status: FROZEN.** This document is the single interface contract for parallel implementation of
M1–M15 + CLI by independent engineers. It is derived from the design spec v1.4 base with the
inline v1.5/v1.6/v1.7/v1.8 revisions (`spec/*.md`), which
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
- Import discipline (no cycles): production imports use only the layered package paths below;
  the former flat modules and `labelkit.config` package do not exist.
  `labelkit.common.contracts.types` and `labelkit.common.errors` import nothing from `labelkit`;
  `labelkit.common.config.model` imports nothing from `labelkit` except
  shared contract types if needed; `labelkit.common.runtime.llm_client` imports only common-layer
  contracts, errors, config, and observability; `labelkit.common.runtime.schema_engine` imports the
  common runtime LLM client plus common errors/observability; `labelkit.common.contracts.stage`
  imports runtime/config/observability types under `typing.TYPE_CHECKING` only. Common never imports
  operators or orchestration. Operator modules import common and declared stdlib/third-party
  dependencies, never orchestration and **never each other** — with the sanctioned lazy-import
  exceptions that `labelkit.operators.verify` calls the public repair surface from
  `labelkit.operators.annotate` (§7.4; used per §7.6) and, v1.8, the public direct-call surfaces
  `labelkit.operators.segment.judge_window` / `labelkit.operators.extract.extract_transition`
  (§7.14/§7.15; used by the stream repair driver, §7.6). Orchestration may import common and
  operators. CLI imports orchestration's public entry points plus common error/config contracts,
  and never imports or instantiates operators.

---

## 1. Package layout and ownership

```text
labelkit/
├── __init__.py                         # __version__ and TOOL_VERSION only
├── cli/
│   ├── __init__.py                     # public exports: main, build_parser, exit_code_for
│   ├── main.py                         # process entry, exception rendering, sole exit-code mapping
│   ├── parser.py                       # argparse definitions and CliOverrides conversion
│   └── commands.py                     # run / validate / rubric user-facing handlers
├── common/
│   ├── contracts/
│   │   ├── types.py                    # Ch.4 shared data types and frame/tree helpers
│   │   └── stage.py                    # Stage protocol and RunContext
│   ├── errors.py                       # cross-layer error vocabulary, exit codes, ErrorKind
│   ├── config/
│   │   ├── __init__.py                 # exports load, default_rubric, ResolvedConfig
│   │   ├── model.py                    # all config dataclasses (M1)
│   │   └── loader.py                   # TOML merge, validation, startup hook validation (M1)
│   ├── runtime/
│   │   ├── llm_client.py               # M9 transport, retry/key pools, concurrency, usage
│   │   └── schema_engine.py            # M8 L0-L3 guarantee, repair, schema validation/stats
│   ├── observability/
│   │   └── obslog.py                   # M12 logs, trace, events, metrics, breaker state
│   └── extensions/
│       └── hooks.py                    # user validator resolution/execution/normalization
├── operators/
│   ├── ingest.py                       # M2
│   ├── segment.py                      # M14
│   ├── dedup.py                        # M3
│   ├── classify.py                     # M13
│   ├── extract.py                      # M15
│   ├── quality.py                      # M4
│   ├── generate.py                     # M6
│   ├── annotate.py                     # M5
│   ├── verify.py                       # M7
│   └── emitter.py                      # M11
├── orchestration/
│   ├── __init__.py
│   ├── orchestrator.py                 # M10 batch/stage lifecycle and report aggregation
│   ├── factory.py                      # operator construction and frozen pipeline order
│   ├── profile_usage.py                # validate --probe referenced-profile discovery
│   └── runtime.py                      # runtime object-graph assembly and public run/validate entry
└── data/rubrics/
    ├── default_text.toml
    ├── default_ui.toml
    └── default_trajectory.toml
```

`labelkit/common/errors.py`, `labelkit/common/contracts/types.py`,
`labelkit/common/contracts/stage.py`, and `labelkit/common/config/model.py` are the canonical homes
of the verbatim frozen material in sections 3–6. Changes to their frozen content still require
updating this file first.

### 1.1 Canonical paths only

The directories above are the only implementation paths. The package root contains only
`labelkit/__init__.py`; the former flat modules (`labelkit.types`, `labelkit.stage`,
`labelkit.errors`, service/operator modules, and `labelkit.orchestrator`) and the former
`labelkit.config` package are intentionally removed. No re-export shim, module alias, or dynamic
forwarder may recreate them. Consumers must import the layered canonical modules.

`labelkit.cli` remains the public module name as the `labelkit/cli/` package; there is no
coexisting `labelkit/cli.py`. Its `__init__.py` exports the established CLI entry surfaces, and the
console-script target `labelkit.cli:main` remains unchanged. Public direct-call surfaces such as
`annotate_record`, `build_*_prompt`, `judge_window`, `extract_transition`, `RunContext`,
`LLMClient`, and `SchemaEngine` retain their frozen signatures and behavior at their canonical
layered paths only.

### 1.2 Test ownership

Offline tests physically mirror the production owners: contracts under `tests/common/contracts/`,
config under `tests/common/config/`, runtime under `tests/common/runtime/`, observability under
`tests/common/observability/`, extensions under `tests/common/extensions/`, operators under
`tests/operators/`, and orchestration under `tests/orchestration/`. Key-pool unit coverage belongs
in `tests/common/runtime/test_llm_client.py`; stream-ingest coverage belongs in
`tests/operators/test_ingest.py`. A separate compatibility-import test, `test_key_pool.py`, or
`test_stream_ingest.py` is forbidden. The exact file allowlist is normative in
`docs/dev/SPEC-package-layer-reorganization.md` §6.1.

---

## 2. Architecture recap (normative)

Four physical layers (spec §2.2 and package-layer reorganization spec):
`labelkit.cli → labelkit.orchestration → labelkit.operators → labelkit.common`. Common contains
cross-layer contracts and shared capabilities, not data-processing business logic: M1 config;
M8/M9 under `common.runtime`; M12 under `common.observability`; user hooks under
`common.extensions`; and the cross-layer error vocabulary at the `common.errors` root. Canonical
files: errors at `labelkit/common/errors.py`; SchemaEngine/LLMClient at
`labelkit/common/runtime/schema_engine.py` and `labelkit/common/runtime/llm_client.py`; hooks at
`labelkit/common/extensions/hooks.py`; obslog at `labelkit/common/observability/obslog.py`. Operators
(M2 ingest, M14 segment, M3 dedup, M13 classify, M15 extract, M4 quality, M5 annotate, M6
generate, M7 verify, M11 emitter) depend only on common, subject solely to the three sanctioned
lazy operator calls (verify→annotate/segment/extract, §7.4/§7.6/§7.14/§7.15). Orchestration may
depend on common and operators and owns construction/order/lifecycle; CLI calls orchestration's
public runtime entry points and owns only parsing, user interaction, and the sole exception-to-exit-
code mapping. Common depends on neither operators nor orchestration; operators never depend on
orchestration; CLI never imports operators.

Pipeline order per batch (process mode, v1.8 chain — the single superset tuple, §7.9):
`segment → dedup → classify → extract → quality → generate(off-path, returns sub-batch) →
annotate → verify → emit`. segment and extract are DEFAULT OFF; with both disabled the chain
degrades byte-identically to the v1.7 chain
`dedup → classify → quality → generate → annotate → verify → emit`. `generate.enabled` and
`segment.enabled` are mutually exclusive (M1, §6.3 rule 29), so the generate slot never
coexists with the stream stages.
Generation sub-batches re-enter the queue as new batches and run
`dedup → classify → quality → annotate → verify → emit` (no generate; single-pass, no recursion;
generated records enter carrying an `"inherited"` Classification, which the idempotent classify
stage skips — §7.13).
`generate_only` mode (v1.4): no M2; `GenerateStage.generate_all()` produces all Records up front,
they are split by `run.batch_size`, and each batch runs `dedup → classify → quality → annotate →
verify → emit` (classify/quality/annotate individually optional per switches; segment/extract
never participate — segment requires process mode, §6.3).

Statuses: `active | dropped_dup | dropped_lowq | dropped_verify | failed | absorbed |
dropped_noise` (the last two are v1.8: `absorbed` = member frame absorbed into an episode
envelope by M14, `dropped_noise` = noise/below-min-len frame dropped by M14 or shrunk out by
M7 member surgery). Stages never delete list elements; they flip `status` and attach evidence.

---

## 3. `labelkit/common/contracts/types.py` — verbatim

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
    "absorbed",        # v1.8 additive: member frame absorbed into a sequence envelope
                       #   (M14 contract ②b, §5/§7.14); THIRD ROUTE — written to neither
                       #   main output nor rejects, counted only (§7.10/§9.3)
    "dropped_noise",   # v1.8 additive: noise/short-segment frame (M14: reason "noise" /
                       #   "below_min_len", §7.14) or verify repair shrink
                       #   (M7: "off_task_member", §7.6) → rejects (§9.2)
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
    kind: Literal["single", "sequence"] = "single"
                                           # v1.8 additive (appended with a default — every
                                           # pre-v1.8 construction site stays unchanged):
                                           # "sequence" = an M14-assembled episode record (§7.14)
    members: tuple["Record", ...] = ()     # v1.8 additive: sequence → member frames in order-key
                                           # ascending order; single → always ().
                                           # Sequence-record field convention (S24, spec §4.1):
                                           # text/raw/ui_tree/image = None; modality = the
                                           # members' modality; id = the sequence rule below
                                           # (fixed at formation — member surgery never
                                           # recomputes it); ref = RecordRef(source_file=first
                                           # member's source, line_no=first member's line_no,
                                           # pair_index=first member's pair_index,
                                           # generated_from=(), generator=None) — full member
                                           # provenance travels in _meta.stream.member_sources
                                           # (§9.1), not in ref
```

**Record id rules (M2/M6/M14, normative):**
- text modality: `sha256(canonical_json(raw).encode("utf-8")).hexdigest()[:16]` where
  `canonical_json(x) = json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`.
- UI modality: `sha256(uitree_file_bytes + image_file_bytes).hexdigest()[:16]`.
- generated records (M6): `raw = {input.text_field: sample_text}`, then the text rule.
- sequence records (M14, v1.8): `sha256("\n".join(member_ids).encode("utf-8")).hexdigest()[:16]`
  over the member ids in order-key ascending order, fixed at episode formation — M7 member
  surgery never recomputes it (spec 3.14.4 step ④).

```python
@dataclass(frozen=True)
class Classification:                      # v1.7: M13 classify verdict (spec 3.13, §4.1)
    label: str                             # routing label of THIS envelope
    labels: tuple[str, ...]                # the record's full hit set (declaration order;
                                           # single assignment: always one element)
    source: Literal["llm", "fallback", "inherited"]
    detail: Mapping                        # reason / sc stats / fallback trace (kind, message)


@dataclass(frozen=True)
class DedupInfo:
    kind: Literal["unique", "exact", "near_text", "near_image", "near_both", "near_semantic"]
    cluster_key: str                       # exact-dedup key ([:16] hex) of the cluster head;
                                           # unique records carry their own key
    kept_id: str | None                    # duplicates: id of the retained record; unique: None


@dataclass(frozen=True)
class Transition:                          # v1.8: one M15 extract verdict for an adjacent member
                                           # pair (spec §4.2), carried by PipelineItem.transitions
    index: int                             # rebuilt ordinal — ALWAYS equals the position in the
                                           # transitions tuple; renumbered after member surgery so
                                           # the invariant len(transitions) == len(members) - 1
                                           # stays true (S31)
    action: Mapping                        # object that passed action_schema (§10.7):
                                           # {action_type, target, value, description} —
                                           # field semantics per the §10.10 table
    model: str                             # provider model string of the extracting profile
    attempts: int                          # 1 + number of L3 repair calls
    detail: Mapping                        # fallback trace: {kind: "extraction_invalid", message}
                                           # (S16); surgery re-seam: {reseamed: True} (S31);
                                           # {} for a clean extraction


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
    defects: tuple[Mapping, ...] = ()      # v1.8 additive (S7): stream defect-table entries
                                           # {"kind", "members", "position", "detail"} — kind is
                                           # the five-value enum of defect_verdict_schema (§10.7);
                                           # non-stream paths: always (); travels to
                                           # _meta.verification.defects (§9.1)


@dataclass(frozen=True)
class StageError:
    stage: str                             # stage name that produced the error
    kind: str                              # error classification code (§7.6 / common.errors.ErrorKind)
    message: str
    retryable: bool


@dataclass
class PipelineItem:                        # the ONLY mutable envelope; lifetime = one batch
    record: Record
    status: Status = "active"
    classification: Classification | None = None   # v1.7: written by M13 classify (or inherited)
    dedup: DedupInfo | None = None
    scores: dict[str, QualityScore] = field(default_factory=dict)
    annotation: Annotation | None = None
    verification: VerificationResult | None = None
    errors: list[StageError] = field(default_factory=list)
    transitions: tuple[Transition, ...] | None = None
                                           # v1.8 additive: written by M15 extract (§7.15);
                                           # None = extract disabled / not reached (idempotency
                                           # gate: `transitions is not None` → skip)
    session_id: str | None = None          # v1.8 additive: in-batch carrier of the session
                                           # boundary (S4) — stamped by M10 on frame envelopes at
                                           # batching, by M14 on the appended episode envelopes
                                           # (bookkeeping, not business logic); M7's repair
                                           # neighborhood query = session_id filter + batch list
                                           # position order


# ── v1.8 shared frame helpers (spec §4.3, S12/S13) ──────────────────────────
# Module-level functions in labelkit/common/contracts/types.py, next to UITree.serialize — the shared
# rendering layer used by M14 segment, M15 extract, M13 classify (sequence
# branch) and M4 quality (sequence branch). Operator modules never depend on
# each other; shared rendering always sinks to this types layer.

def frame_digest(record: Record, max_chars: int) -> str:
    """Best-effort deterministic frame digest (S12 — UINode is a closed nine-field
    type; package/activity names are reachable only via `extra`):
    - UI modality:
        app      = first non-empty `extra` value among package|package_name|pkg
                   (visible nodes);
        activity = first non-empty `extra` value among
                   activity|activity_name|window_title (may be absent);
        title    = first visible non-empty text in DFS order;
        salient  = visible text/content_desc de-duplicated in encounter order;
                   Button/EditText/CheckBox-class interactive roles get a "*" prefix;
      the whole digest is truncated to max_chars (serialize truncation convention).
    - text modality: record.text truncated to max_chars.
    Poverty judgment: zero visible text nodes, or digest length < 8 ⇒ poor — the
    CALLER counts digest_poor_frames (report.stream, §9.3) and WARNs at most once
    per run, pointing at segment.use_vision."""
    ...


def tree_diff(a: UITree | None, b: UITree | None, quantize_px: int) -> Mapping:
    """Structural-key MULTISET matching over (role, bounds // quantize_px, depth)
    (S13 — node_id is NOT a cross-frame identity and must not be used as a match
    key); visible nodes only; O(n1 + n2); pure statistics, no semantic attribution
    (attribution belongs to M15). Returns:
    {added: int, removed: int, text_changed: int, change_ratio: float,
     app_changed: bool, title_changed: bool}."""
    ...
```

Notes binding on all implementers:

- `QualityScore.score` is `float | None` — the spec's `on_unscored` path requires representing
  "score = null" (spec 3.4.3 判定失败 row, §6.3 example semantics). **[FROZEN HERE]**
- `Annotation.sc` is an additive v1.2 field needed to carry `{n, agreement_ratio}` from M5 to M11
  (`_meta.annotation.sc`, spec 3.5.2/6.3). **[FROZEN HERE]**
- `Classification` / `PipelineItem.classification` are additive v1.7 fields (M13, spec §4.1).
  Multi-assignment fan-out clones share `record` and `dedup` **by reference** with their
  original envelope; all other containers are fresh defaults (§7.13).
- v1.8 additive fields (spec §4.1/§4.2, all appended with defaults — zero changes at
  pre-v1.8 construction sites): `Record.kind`/`Record.members`, `Transition`,
  `VerificationResult.defects`, `PipelineItem.transitions`/`PipelineItem.session_id`, and the
  two `Status` values `absorbed`/`dropped_noise`. Sequence records hold their members **by
  reference** (frozen objects shared, zero copy) — episode formation does not change the
  batch's memory order of magnitude (spec §2.6).
- `frame_digest`/`tree_diff` are v1.8 module-level helpers whose docstrings above are the
  behavior contract (spec §4.3 末段); M14/M15/M13/M4 consume them — never re-implement
  digest/diff logic inside an operator module.
- Everything except `PipelineItem` is `frozen=True`. No module mutates a `Record`.

---

## 4. `labelkit/common/errors.py` — verbatim

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
    """M9: retryable provider error with retries exhausted (v1.6: incl. park-budget overrun,
    run.max_park_s). Record-level → status='failed'."""
    def __init__(self, message: str, profile: str, retries: int,
                 key_env: str | None = None):
        self.profile = profile
        self.retries = retries
        self.key_env = key_env                # v1.6: env-var NAME of the last key tried (pools)
        super().__init__(message)


class ProviderFatalError(LabelKitError):
    """M9: non-retryable provider error (401/403/400/404, dims mismatch). Feeds the circuit
    breaker; a streak >= run.fatal_error_threshold ends the run with exit code 4.
    v1.6 pools: an auth failure absorbed by key rotation raises nothing — this exception is
    raised for auth only when the LAST live key gets disabled (spec 3.9.3)."""
    def __init__(self, message: str, profile: str, status_code: int | None = None,
                 key_env: str | None = None):
        self.profile = profile
        self.status_code = status_code
        self.key_env = key_env                # v1.6: env-var NAME of the failing key (pools)
        super().__init__(message)


class SchemaViolation(LabelKitError):
    """M8: L3 budget exhausted, object still invalid. Record-level → status='failed',
    kind='schema_violation' — or 'callback_violation' when the remaining violations
    all come from the output.validator hook (callback_only=True, spec 3.8.2 L2.5)."""
    def __init__(self, errors: list[str], raw_last_output: str, *,
                 callback_only: bool = False):
        self.errors = errors                  # rendered violations: "<json-pointer>: <message>"
        self.raw_last_output = raw_last_output
        self.callback_only = callback_only
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
    SEGMENTATION_INVALID = "segmentation_invalid"            # v1.8: M14, window-level — M8 repair
                                                             # exhausted; "keep" (default) keeps the
                                                             # session alive as ONE whole episode
                                                             # (evidence in _meta.stream.degraded +
                                                             # error event + segment.failures, NEVER
                                                             # in item.errors — S26); "fail" → all
                                                             # session members failed → rejects
    CLASSIFICATION_INVALID = "classification_invalid"        # v1.7: M13, M8 repair exhausted —
                                                             # fallback keeps record; "fail" → rejects
    EXTRACTION_INVALID = "extraction_invalid"                # v1.8: M15, transition-level — M8 repair
                                                             # exhausted; "fallback" (default) records
                                                             # the step as action_type="other"
                                                             # (evidence in Transition.detail =
                                                             # {kind, message}, episode stays alive,
                                                             # NEVER in item.errors — S16); "fail" →
                                                             # episode failed → rejects
    JUDGMENT_INVALID = "judgment_invalid"                    # M4, comparison-level → counts as tie
    SCHEMA_VIOLATION = "schema_violation"                    # M8 L3 exhausted → failed → rejects
    CALLBACK_VIOLATION = "callback_violation"                # v1.5: L3 exhausted, remaining
                                                             # violations all from output.validator
    PROVIDER_RETRYABLE_EXHAUSTED = "provider_retryable_exhausted"  # M9 → failed, feeds breaker window
    PROVIDER_FATAL = "provider_fatal"                        # M9 run-level, feeds breaker directly
    INTERNAL_ERROR = "internal_error"                        # any unexpected exception
```

Exception → exit-code mapping is implemented **only** in `labelkit/cli/main.py` (§7.12). No module calls
`sys.exit`.

---

## 5. `labelkit/common/contracts/stage.py` — verbatim

```python
"""Stage protocol (spec §4.3) and RunContext (spec §3.10.3). Frozen contract."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from labelkit.common.config.model import ResolvedConfig
    from labelkit.common.runtime.llm_client import LLMClient
    from labelkit.common.runtime.schema_engine import SchemaEngine
    from labelkit.common.observability.obslog import MetricsSink
    from labelkit.common.contracts.types import PipelineItem


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
           ②a（v1.7）classify 例外（仅 assignment="multi"）——可向传入列表尾部追加派生信封；
           追加物视同批内普通元素、同受 ①③④ 约束；不得删除、重排或替换任何既有元素对象
           （既有元素的 status / classification / errors 字段写入属 ①④ 的正常行为）；
           返回值仍须是传入的同一列表对象（调用方依赖列表身份）；
           ②b（v1.8）segment 例外（仅 stream 模式）——segment 可将批内既有 active 成员信封的
           status 置为 `absorbed` 或 `dropped_noise`（属①④的正常状态写入），并向传入列表
           **尾部**追加以这些成员拼装的序列信封；追加物视同批内普通元素、同受①③④约束；
           每个成员信封至多被一个序列信封吸收；不得删除、重排或替换任何既有元素对象；
           返回值仍须是传入的同一列表对象。**M7 修复路径豁免**：verify 的缺陷修复可在本批内
           将成员信封状态在 `absorbed` 与 `dropped_noise` 间双向改写（成员回收/收缩），
           此为契约①的唯一反向豁免；禁止将成员信封翻回 `active`；
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
  v1.7 (contract ②a): classify may grow that list in place (tail-append only); identity of the
  returned list is unchanged.
- v1.8 (contract ②b): segment may flip existing active member envelopes to
  `absorbed`/`dropped_noise` and tail-append sequence envelopes assembled from them; each member
  envelope is absorbed by AT MOST one sequence envelope. The M7 repair-path exemption is the
  ONLY sanctioned reverse status write in the whole contract: verify's defect repair may rewrite
  member envelopes bidirectionally between `absorbed` and `dropped_noise` (member reclaim /
  shrink) WITHIN the current batch; flipping a member back to `active` is forbidden — a frame
  and its episode must never both reach the main output.
- Non-generate stages: return value must be the input list (callers may rely on identity).
- A stage must catch every per-record exception, append
  `StageError(stage=self.name, kind=..., message=..., retryable=...)` to `item.errors`, set
  `status="failed"`, emit the `error` trace event, and continue. Only `CircuitBreakerTripped`,
  `KeyboardInterrupt`/`CancelledError` may escape a stage.

---

## 6. `labelkit/common/config/` — M1

### 6.1 `labelkit/common/config/model.py` — verbatim dataclasses

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
    api_key_envs: tuple[str, ...] = ()            # v1.6 key pool (spec 3.9.3): TOML accepts
                                                  # exactly one of api_key_env/api_key_envs;
                                                  # M1 normalizes BOTH forms into this tuple
                                                  # (scalar → 1-tuple) — always non-empty after
                                                  # load; api_key_env mirrors element 0
    api_keys: tuple[str, ...] = field(default=(), repr=False)
                                                  # v1.6: resolved values aligned with
                                                  # api_key_envs; NEVER logged; api_key mirrors
                                                  # element 0 for single-key readers


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
    api_key_envs: tuple[str, ...] = ()            # v1.6 key pool — same normalization as
                                                  # LLMProfile.api_key_envs
    api_keys: tuple[str, ...] = field(default=(), repr=False)   # v1.6, NEVER logged


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
    max_park_s: int = 3600                        # v1.6 (spec 3.9.3/5.2): park budget per logical
                                                  # LLM call while a profile's whole key pool is
                                                  # cooling; 0 = no parking; overrun → the normal
                                                  # retry-exhaustion path (feeds the breaker)


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
    rubric: str = ""                              # "default:text"|"default:ui"|
                                                  # "default:trajectory" (v1.8)|"inline";
                                                  # "" = auto by modality (M1 resolves);
                                                  # v1.8 (S29): "" under segment.enabled = true
                                                  # resolves to "default:trajectory" instead
                                                  # (both modalities; an explicit selector always
                                                  # wins; class views inherit via base selector)
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
    sequence_frames: int = 20                     # v1.8: max keyframes per sequence-annotation
                                                  # request, ∈ [2, 100] (M1; outside → CONFIG_
                                                  # ERROR). n members > k → deterministic uniform
                                                  # downsample idx_i = ⌊i·(n−1)/(k−1)⌋, i=0..k−1
                                                  # (first/last always kept, strictly increasing,
                                                  # pure integer zero-rng; n <= k takes all —
                                                  # S28). > 20 while the annotate profile's
                                                  # max_image_px > 2000 → M1 WARN (§6.3);
                                                  # explicitly set while non-stream → no-op
                                                  # warning


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
                                                  # allowed: ingest|segment|dedup|classify|
                                                  # extract|quality|annotate|verify|schema|llm —
                                                  # TEN values (v1.7 adds "classify"; v1.8 adds
                                                  # "segment"/"extract": channel = stage name,
                                                  # S1); the default stays unchanged
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


# ── classify (v1.7, spec §5.2 [classify] + [class.*]) ──────────────────────

@dataclass(frozen=True)
class ClassSpec:
    name: str                                     # [a-z0-9_]+, unique within the table
    description: str                              # non-empty
    examples: tuple[str, ...] = ()                # optional, input-side only

@dataclass(frozen=True)
class ClassifyConfig:
    enabled: bool = False
    llm: str = "default"
    assignment: Literal["single", "multi"] = "single"
    max_labels: int | None = None                 # M1 back-fills to len(classes)
    instruction: str = ""
    fallback_class: str = ""                      # required iff enabled; must be in classes
    self_consistency: int = 0                     # 0 = off; else odd, >= 3
    sc_temperature: float = 0.7                   # effective only when sc >= 3 (R21)
    on_error: Literal["fallback", "fail"] = "fallback"
    classes: tuple[ClassSpec, ...] = ()           # >= 2 entries iff enabled

@dataclass(frozen=True)
class ClassView:                                  # one class's effective config;
                                                  # class_views = {} when enabled=false
    name: str
    quality: QualityConfig                        # selection-GROUP merge semantics (R6);
                                                  # rubric selector already back-filled
    rubric: Rubric                                # re-parse product (R7)
    annotate: AnnotateConfig
    generate: GenerateConfig
    verify: VerifyConfig
    extract: ExtractConfig                        # v1.8 (S2) — REQUIRED sixth field, no default:
                                                  # per-class effective extract config (whitelist:
                                                  # only `instruction` may differ from global,
                                                  # §6.3 rule 35); `_merge_class_sections` grows
                                                  # from a four- to a five-section tuple. segment
                                                  # has NO per-class view: it runs BEFORE classify,
                                                  # labels do not exist yet (chain-order causality,
                                                  # spec §5.2)


# ── stream (v1.8, spec §5.2 [stream] + [segment] + [extract]) ───────────────

@dataclass(frozen=True)
class StreamConfig:                               # input-side ordering + sessionization
                                                  # declaration, consumed by M2 (§7.1); effective
                                                  # only under segment.enabled (presence while
                                                  # disabled → no-op warning, §6.3)
    order_by: str = "input_order"                 # "input_order" (text: filename lexicographic →
                                                  # line_no; UI: pair_index ascending) |
                                                  # "meta:<field>" (TEXT MODALITY ONLY; timestamp
                                                  # parsing per spec §6.1 / S20 — see §7.1)
    on_disorder: Literal["skip", "fail"] = "skip" # skip: record skipped, counts bad_input +
                                                  # IngestReport.disorder + ingest.disorder event
                                                  # + ONE stderr WARN per run; fail: InputError →
                                                  # exit 3. Monotonicity cursors are maintained
                                                  # PER PARTITION KEY (S19)
    key: tuple[str, ...] = ()                     # partition keys; key change = session break
                                                  # (groupby semantics, NOT keyBy — input must
                                                  # arrive grouped by key); elements:
                                                  # "meta:<field>" (text only) | "source_dir"
                                                  # (= ref.source_file parent dir, UI-capable,
                                                  # S19)
    gap_s: int = 300                              # break when adjacent time delta > gap_s seconds;
                                                  # may be SET only under order_by="meta:*" (M1).
                                                  # Default is deliberately large: under-splitting
                                                  # is recoverable by LLM refinement,
                                                  # over-splitting is not (spec §5.2)
    gap_steps: int = 0                            # break when adjacent ordinal delta > gap_steps;
                                                  # 0 = off; combinable with gap_s (either fires)
    session_max_len: int = 200                    # hard cap (frames), break at limit;
                                                  # > run.batch_size → M1 static WARN (S21)
    session_max_span_s: int = 0                   # hard time-span cap (seconds; 0 = off); may be
                                                  # SET only under order_by="meta:*" (M1)


@dataclass(frozen=True)
class SegmentConfig:                              # M14 (§7.14) — the stream-mode master switch
    enabled: bool = False                         # false = stage not in chain; output
                                                  # byte-identical to v1.7 except the always-
                                                  # present _meta.stream: null (§9.1). Enabling
                                                  # requires process mode + generate off +
                                                  # annotate on (§6.3 rule 29)
    strategy: Literal["rules", "llm", "hybrid"] = "hybrid"
                                                  # rules: candidate sessions become episodes
                                                  # as-is, ZERO LLM (noise_filter/min_len
                                                  # ineffective); llm/hybrid: sliding-window
                                                  # refinement — identical behavior inside M14
                                                  # (rule-layer sessionization is always on in M2;
                                                  # "hybrid" names the rules+LLM composition)
    llm: str = "default"                          # joins the four reference sets ONLY when
                                                  # strategy ∈ {llm, hybrid} (S30, §6.3 rule 33)
    window: int = 20                              # sliding-window frames per call; M1: >= 2;
                                                  # step = window − 1 (1-frame overlap; window >=
                                                  # session length degrades to one whole-session
                                                  # call, S32)
    digest_max_chars: int = 400                   # frame_digest truncation cap (§3)
    noise_filter: bool = True                     # llm/hybrid only; rules + explicit true →
                                                  # no-op warning (§6.3)
    min_len: int = 2                              # segment length floor; applies ONLY to LLM-
                                                  # refined segments (S11) — rule-layer lone-frame/
                                                  # short sessions become episodes untouched;
                                                  # dropped frames get reason "below_min_len"
                                                  # (≠ "noise"), counted separately (§9.3)
    use_vision: bool = False                      # true: attach per-frame screenshots inside
                                                  # window calls (profile joins the vision set,
                                                  # S30); default = pure-text verdicts
    context: str = ""                             # optional domain context injected into the
                                                  # §10.9 template — NOT a boundary definition
                                                  # (the criteria are built in; zero-config works)
    on_error: Literal["keep", "fail"] = "keep"    # keep (default): whole session degrades to ONE
                                                  # episode + _meta.stream.degraded evidence
                                                  # (never item.errors — S26); fail: session
                                                  # members failed → rejects (§4
                                                  # segmentation_invalid)


@dataclass(frozen=True)
class ExtractConfig:                              # M15 (§7.15); UI-modality sequences only
    enabled: bool = False                         # requires segment.enabled AND
                                                  # run.modality = "ui" (§6.3 rule 30)
    llm: str = "default"                          # when enabled: ALWAYS in all four reference
                                                  # sets AND always in the vision set — every
                                                  # request carries 2 images, no text-only tier
                                                  # (S30)
    instruction: str = ""                         # optional domain hint appended to the §10.10
                                                  # system message; the ONLY key overridable via
                                                  # [class.<name>.extract] (§6.3 rule 35)
    include_diff: bool = True                     # inject [树变更摘要] (tree_diff rendering) into
                                                  # the extract prompt (S14: structural tree diff,
                                                  # NOT pixel diff); false = A/B ablation
                                                  # (observable via extract.by_type, §9.3)
    on_error: Literal["fallback", "fail"] = "fallback"
                                                  # fallback (default, S16): the step records
                                                  # action_type="other" + Transition.detail =
                                                  # {kind, message} (never item.errors); fail:
                                                  # episode failed → rejects (§4
                                                  # extraction_invalid)


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
    stream: StreamConfig                          # v1.8 — required, no default (R23 convention;
                                                  # every construction site passes keywords)
    dedup: DedupConfig
    segment: SegmentConfig                        # v1.8 — required, no default
    extract: ExtractConfig                        # v1.8 — required, no default
    classify: ClassifyConfig                      # v1.7 — required, no default (R23)
    quality: QualityConfig
    generate: GenerateConfig
    annotate: AnnotateConfig
    verify: VerifyConfig
    output: OutputConfig
    trace: TraceConfig
    rubric: Rubric                                # resolved (default pkg or inline)
    class_views: Mapping[str, ClassView]          # v1.7 — required, no default (R23);
                                                  # frozen per-class merged views, keyed by
                                                  # class name; {} when classify disabled
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
(`"default:text"` / `"default:ui"`) — v1.8 (S29): when `segment.enabled = true` the empty
selector resolves to `"default:trajectory"` instead, both modalities, explicit selectors
untouched; resolve `trace.path` default; resolve `run.input`/`run.output`
CLI overrides; parse `output.schema_inline`/`schema_path` into `user_schema`; read every
*referenced* profile's declared key env vars (`api_key_env`, or each element of
`api_key_envs`) into `LLMProfile.api_keys`, mirroring element 0 into `api_key` (v1.6
normalization, §6.3 rule 12); `tool.log_level` overridden by
`--log-level`. Precedence: CLI > project.toml > config.toml/built-in defaults.
v1.7: statically merge every `[class.<name>.*]` override family into the frozen
`class_views` mapping (per-key provenance; selection-group and rubric re-parse semantics of
§6.3 rules 26–27) and back-fill `classify.max_labels` to `len(classes)` when absent. The
per-class merge is project.toml-INTERNAL conditionalization; the three-source precedence
above is unchanged. v1.8: the merge covers the fifth section `extract` (whitelist:
`instruction` only, §6.3 rule 35) and every `ClassView` carries the required `extract` field
(S2); per-class rubric re-resolution inherits the S29 empty-selector rule through the base
selector automatically.

### 6.2 `labelkit/common/config/loader.py` — API (spec 3.1.3, verbatim)

```python
def load(config_path: Path, project_path: Path, cli_overrides: CliOverrides) -> ResolvedConfig:
    """Three-source merge + full validation. On failure raises ConfigError(errors: list[str])
    carrying ALL errors (never first-only); CLI exits 2."""

def default_rubric(name: Literal["default:text", "default:ui",
                                 "default:trajectory"]) -> Rubric:
    """Load a packaged default rubric from labelkit/data/rubrics/*.toml
    (importlib.resources). "default:trajectory" is v1.8 (default_trajectory.toml,
    spec Appendix A.3, rubric name "default-trajectory-v1")."""
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
   v1.8: under `segment.enabled = true` this rule is superseded by the per-stage vision table
   of rule 34 (quality is exempted there; classify/extract/segment join per their own rows).
5. `dedup.semantic = true` ⇒ `dedup.semantic_embedding` set, exists in `[embedding.*]`, and that
   profile passes rule 12's key check (exactly one of `api_key_env`/`api_key_envs`, every
   listed variable set and non-empty; v1.6).

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
    non-empty. Unreferenced profiles are not checked. v1.6 key pool (spec 3.1.4/5.1): exactly
    one of `api_key_env` / `api_key_envs` is provided (both or neither → error);
    `api_key_envs` must be a non-empty array of non-empty, distinct env-var names; for a
    referenced profile EVERY listed variable must exist and be non-empty (one aggregated
    error line per missing variable). M1 normalizes the scalar form to a 1-tuple so
    `api_key_envs`/`api_keys` are always populated after load (§6.1).

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

Classify (v1.7, spec 3.1.4 分类/按类覆盖合并 rows + 5.2 whitelist table; all checks below
apply only when `classify.enabled = true` unless stated):
22. `[[classify.classes]]` has ≥ 2 entries; each `name` matches `[a-z0-9_]+` and is unique
    within the table; each `description` is non-empty; `examples`, when present, is an array
    of strings (input-side only). `classify.fallback_class` is required and must be one of
    the class names.
23. `classify.llm` must exist in `[llm.*]`; UI modality ⇒ that profile has
    `supports_vision = true`. The classify profile joins ALL THREE reference sets (R24):
    the loader's referenced set (rule 12 key resolution), the vision-check set (rule 4),
    and `labelkit.orchestration.profile_usage.referenced_profiles()` (`validate --probe`).
24. `classify.assignment` ∈ {"single", "multi"}; `classify.max_labels` may be set ONLY when
    `assignment = "multi"` and must be ∈ [2, len(classes)] — when absent M1 back-fills it to
    `len(classes)`. `classify.self_consistency` is 0 or an odd integer ≥ 3;
    `classify.on_error` ∈ {"fallback", "fail"}.
25. `[class.<name>.*]`: `<name>` must be a declared class name. Override keys must be inside
    the per-section whitelist — `quality`: mode, rounds, rubric (incl. the `[class.*.rubric]`
    inline table), threshold, selection, top_ratio; `annotate`: instruction, examples;
    `generate`: instruction, styles, num_per_record, temperature; `verify`: extra_criteria.
    Any key outside the whitelist → CONFIG_ERROR (R25 exception to rule 1's unknown-key
    warning: `[classify]` / `[class.*]` are explicitly owned namespaces).
26. Per-class merge builds the frozen `class_views` (per-key provenance: keys the class
    provides override the global section, all others inherit). Selection GROUP (R6): a class
    providing ANY of selection/threshold/top_ratio evicts the global side's mutually-
    exclusive counterpart keys from the merged view; the rule-6 exclusivity check runs on
    each class's MERGED view (never on the raw key union).
27. Per-class rubric (R7): merge the selector, then RE-PARSE via the `_resolve_rubric`
    helper; the rule-16 pointwise 6-level check runs on every (class-effective mode ×
    class-effective rubric) combination; `[class.X.rubric]` present while that class's
    effective selector is not `"inline"` → table ignored + warning (same convention as the
    global rubric).
28. Every `[[class.<name>.annotate.examples]]` output dry-runs against the GLOBAL user
    schema and `output.validator` (rule 15 semantics; error locations rendered
    `[[class.<name>.annotate.examples]][N]`, N 1-based).

Stream (v1.8, spec §5.2 [stream]/[segment]/[extract] rows + spec 2.3.1; all checks below
apply only when the named switch is on unless stated):
29. `segment.enabled = true` requires ALL of: `run.mode = "process"`, `generate.enabled =
    false` (generate_only is excluded transitively — rule 10 requires `generate.enabled =
    true` there, so stream × generate_only can never co-validate), and
    `annotate.enabled = true` (sequence records have no passthrough output form).
30. `extract.enabled = true` requires `segment.enabled = true` AND `run.modality = "ui"`
    (text sequences are out of scope in v1).
31. `[stream]` fields: `stream.order_by` ∈ {`"input_order"`, `"meta:<field>"`};
    `order_by = "meta:*"` is TEXT-MODALITY-ONLY; explicitly setting `stream.gap_s` or
    `stream.session_max_span_s` requires `order_by = "meta:*"`; every `stream.key` element
    is `"meta:<field>"` (text modality only) or `"source_dir"` (either modality).
32. `segment.window >= 2`; `2 <= annotate.sequence_frames <= 100` (outside the range →
    CONFIG_ERROR).
33. Reference sets (S30 — the "three sets" of rule 23 are FOUR for v1.8 profiles:
    key resolution (rule 12) / vision (rule 4/34) / `validate --probe`
    (`labelkit.orchestration.profile_usage.referenced_profiles()`) / existence): `segment.llm`
    joins them ONLY when
    `segment.enabled` AND `segment.strategy ∈ {llm, hybrid}` (the rules strategy makes zero
    LLM calls — no key may be demanded), and joins the vision set only when
    `segment.use_vision = true`; `extract.llm`, when `extract.enabled`, ALWAYS joins all
    four sets and ALWAYS the vision set (every extract request carries 2 images).
34. Stream-mode per-stage vision table (S30; UI modality, `segment.enabled = true`):
    classify ✓ (first-frame screenshot, §10.8), annotate ✓ (multi-image, §10.1),
    verify ✓ (first/last-frame screenshots, §10.5), extract ✓ (always), segment — only when
    `use_vision = true`, **quality ✗** — sequence scoring is pure text (§10.2/§10.3 sequence
    variants); `quality.llm` is the single vision relaxation of rule 4.
35. `[class.<name>.extract]` whitelist: `instruction` ONLY (extends rule 25's table; any
    other key → CONFIG_ERROR). `[class.<name>.segment]` does NOT exist as a section:
    segment runs BEFORE classify, class labels do not exist at segmentation time
    (chain-order causality, spec §5.2 note) — it is outside rule 25's section list, so any
    such table falls to the whitelist CONFIG_ERROR.
36. Rubric selector enumeration is `"default:text" | "default:ui" | "default:trajectory"
    (v1.8, packaged default_trajectory.toml) | "inline"`; empty-selector resolution per S29:
    `segment.enabled = true` ⇒ `""` → `"default:trajectory"` (both modalities; explicit
    selectors always win; class views inherit through the base selector). Rules 16/26/27
    apply to the trajectory rubric unchanged.

Warnings (non-blocking): `verify` enabled and `verify.llm`'s `model` equals `annotate.llm`'s
`model` → warn about self-enhancement bias (spec 3.7.2). v1.7 (R8): `classify.enabled = false`
while `[[classify.classes]]` and/or `[class.*]` tables are present → ONE warning naming the
ignored tables, never a CONFIG_ERROR ("keep the config, flip the switch" is legal — same
family as the ineffective-top_ratio warning). v1.8 additions (same R8 family, all
non-blocking): any of `[stream]`/`[segment]`/`[extract]` present while `segment.enabled =
false` → ONE warning naming the ignored tables; `segment.strategy = "rules"` with explicit
`noise_filter = true` → no-op warning; `annotate.sequence_frames` explicitly set while
`segment.enabled = false` → no-op warning; effective trajectory rubric while
`extract.enabled = false` → warning (the rubric is modality-neutral and does not presuppose
steps — "步骤" degrades to "帧间变化", S29); `stream.session_max_len > run.batch_size` →
static WARN (S21: such sessions will be hard-split by M10 + `session_split` mark);
`annotate.sequence_frames > 20` while the annotate profile's `max_image_px > 2000` → WARN
(S28: Anthropic hard-rejects >20-image requests containing any image over 2000 px — HTTP
400, not a resize; the default max_image_px=2048 hits it. Guide: set `max_image_px <= 2000`
or lower `sequence_frames`; the 20-image threshold counts ALL image blocks in the request).

---

## 7. Module public APIs

Everything in this section is the complete public surface. Anything not listed is private
(`_`-prefixed) and may not be imported across modules.

### 7.1 M2 — `labelkit/operators/ingest.py`

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
    session_lens: tuple[int, ...] = ()             # v1.8 (S23): session dry-run lengths, filled by
                                                   # scan(estimate=True) only when segment.enabled
                                                   # (single read pass fused with the line count);
                                                   # estimate=False or non-stream: () — feeds the
                                                   # M10 _estimate stream formulas (§7.9)


@dataclass                                         # mutable counters   [FROZEN HERE]
class IngestReport:
    scanned: int = 0                               # lines seen / pair indexes seen
    ingested: int = 0
    bad_input: int = 0                             # bad lines + skipped conflicts + missing pairs
                                                   # (v1.8: + skipped disorder records)
    missing_pair: int = 0                          # UI only
    index_conflict: int = 0                        # UI only
    sessions: int = 0                              # v1.8: candidate sessions closed by the
                                                   # assembler (stream mode only; data source of
                                                   # report.stream.sessions, §9.3)
    disorder: int = 0                              # v1.8: records skipped by the monotonicity
                                                   # check (out-of-order or timestamp parse
                                                   # failure; a SUBSET of bad_input, S20)
    bad_locations: list[dict] = field(default_factory=list)
                                                   # {"file": str, "line_no": int|None,
                                                   #  "index": int|None, "reason": str}


@dataclass(frozen=True)                            # v1.8 [FROZEN HERE]
class Session:
    session_id: str                                # sha256("\n".join(record ids))[:16] over the
                                                   # session's records in session order
                                                   # [FROZEN HERE, see §12]
    records: tuple[Record, ...]                    # session members in session (order-key) order
    cause: Literal["gap", "key", "max_len", "max_span", "eof", "limit"]
                                                   # what closed the session (spec 3.2/S17
                                                   # vocabulary; = segment.session payload cause)


class Ingestor:
    def __init__(self, cfg: ResolvedConfig): ...

    def scan(self) -> IngestPlan:
        """Scan only, no parsing: file list, pairing table, estimated record count.
        Used by --dry-run and `validate`. Raises InputError if run.input is missing/unreadable
        or (UI, on_index_conflict='fail') a conflict is found. v1.8 (S23): in stream mode,
        text-modality estimate=True fuses line counting and the session dry-run into a
        SINGLE pass (no second full read)."""

    def records(self) -> Iterator[Record]:
        """Lazy Record stream. Parse errors follow input.on_bad_line / on_missing_pair /
        on_index_conflict ('skip' → count + trace event; 'fail' → raise InputError).
        Emits trace events ingest.bad_line / ingest.missing_pair / ingest.index_conflict via
        the metrics sink handed to it (see below). Non-stream entry point — unchanged."""

    def sessions(self) -> Iterator[Session]:
        """v1.8 (stream mode): the SESSION-STREAM VIEW consumed by M10 instead of records().
        Pipeline: parse stream (= records() semantics, incl. ordering per stream.order_by
        and the per-partition-key monotonicity check with stream.on_disorder, S19/S20) →
        frame-level --limit islice HERE, between the parse stream and the assembler (S17;
        the limit unit stays FRAMES, never sessions) → rule-layer session assembler
        (stream.key change / gap_s / gap_steps / session_max_len / session_max_span_s —
        any trigger closes the session). Emits one `segment.session` trace event per closed
        session (owner M2; the segment.* prefix routes it to the segment channel, S1) and
        counts IngestReport.sessions. --limit truncation is treated as EOF: the unclosed
        tail session is flushed with cause="limit" + ONE stderr WARN (S17). cause="limit"
        means "closed WHERE the --limit budget ran out" — budget exhaustion exactly
        at EOF is indistinguishable from real truncation without pulling (and
        parsing) one extra record, which would perturb the scanned/bad_input
        ledger; the tool does not disambiguate, and the WARN states budget
        exhaustion rather than claiming truncation (v1.8 D3). A source-FILE
        change under text input_order ALSO closes the session with cause="key"
        (line_no ordering does not extend across files; under meta:* ordering
        file boundaries are transparent — v1.8 D7)."""

    @property
    def report(self) -> IngestReport: ...
```

Wiring note **[FROZEN HERE]**: `Ingestor` is not a `Stage` and has no `ctx`;
`labelkit/orchestration/runtime.py` sets `ingestor.metrics = metrics_sink` (public attribute,
default `None`) before the orchestrator calls `records()` so ingest trace events can be emitted
with `batch_no=0`.

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

v1.8 stream ordering & monotonicity (spec §6.1, S19/S20 — active only when `segment.enabled`):

- **Ordering.** `stream.order_by = "input_order"` (default): text = filename lexicographic →
  line number; UI = pair_index ascending. `"meta:<field>"` (text only): `<field>` is a dotted
  path on the raw line object; timestamp parsing — numeric: `v < 0 ∨ v >= 1e14` ⇒ parse
  failure, `v < 1e11` ⇒ epoch SECONDS, `1e11 <= v < 1e14` ⇒ epoch MILLISECONDS (÷1000);
  string: try pure-number → numeric rules, then `datetime.fromisoformat` (Python 3.11 accepts
  the `Z` suffix), both fail = parse failure; timezone-aware values convert to UTC epoch,
  naive values are INTERPRETED AS UTC; the internal sort key is float seconds (S20).
- **Streaming monotonicity check** (no full re-sort): one cursor PER `stream.key` partition
  key (memory = key cardinality, S19) — per-device/per-source concatenated inputs are not
  falsely flagged; key change = session break (groupby semantics: input must arrive grouped
  by key). Out-of-order records and parse failures BOTH follow `stream.on_disorder`:
  `"skip"` (default) = skip + count `bad_input` + `IngestReport.disorder` + one
  `ingest.disorder` event per record (trace-only; M2 itself logs ONE data-free stderr
  WARN per run — the reason carries timestamp values and never reaches stderr, §8.1);
  `"fail"` = InputError → exit 3.

### 7.2 M3 — `labelkit/operators/dedup.py`

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

v1.8 sequence records (S10 — episode-level duplicate = "the same operation flow"; four
adaptation points, all others unchanged):

- **Dedup text.** The dedup-text recipe gains a `kind == "sequence"` branch that takes
  precedence over the modality branch: the MEMBERS' single-record recipes (text rule / tree
  serialization, per modality) concatenated in member order with separator `"\x1e"` (ASCII
  Record Separator — `isspace() == True`, structurally collision-free against
  whitespace-collapsed normalized text) **[FROZEN HERE: the separator]**. Levels ①② run on
  that concatenation.
- **Level ③ (pHash)** auto-skips sequence records (their `image is None` — the existing
  gate); with `ui_dup_requires = "both"` the composite verdict degrades to tree-only for
  sequence records (the image_decode_failed degradation path, spec 3.3.4).
- **Level ④ (semantic)** participation/verdict-kind logic gains the sequence case ("both"
  walks the tree-only branch); an over-long embedding input that fails after retries takes
  the EXISTING `embedding_failures` skip path (spec 3.3.3 — no new failure route).
- Member frames never reach M3 individually (they are `absorbed`/`dropped_noise` before
  dedup in the chain, §7.9) — frame-level dedup semantics are intentionally void in stream
  mode.

### 7.3 M4 — `labelkit/operators/quality.py`

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

Normative behavior (spec 3.4.3/3.4.4): comparison pool = the per-class pool within the batch —
active items partitioned by `item.classification.label` (v1.7; classify disabled ⇒ ONE anonymous
pool = the whole batch, byte-identical to pre-v1.7 behavior); k = `rounds` independent
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

v1.7 per-class pooling (classify enabled; spec 3.4.3 按类分池 row):

- **Two-phase execution (R13).** Phase 1, synchronous: iterate the pools in class-name
  lexicographic order and pre-draw each pool's full pairing plan (this is the only `ctx.rng`
  consumption — the consumption ORDER is therefore pool-order-deterministic); phase 2: merge
  every pool's LLM judging calls into ONE `asyncio.gather` (full cross-pool concurrency).
  Internally `_run_pairwise` splits into plan/dispatch phases.
- **Per-pool effective config.** Each pool reads `class_views[label]`'s (QualityConfig, Rubric):
  mode/rounds/rubric/threshold/selection/top_ratio take the class-effective values;
  judges/both_orders/criteria_per_call/llm/on_unscored always stay global.
- **Pool-level isolation (R15).** The batch-level internal-error fallback wraps EACH pool
  (try/except inside the pool loop): pool A's failure never voids pool B's finished scores.
- The batch-of-1 rule above applies PER POOL (a pairwise pool of 1 scores 0.5 with no calls);
  `top_ratio` quota base = scored survivors WITHIN the pool; normalization ranks within the pool.
- Counters and events gain the pool dimension: tie counters become
  `quality.tie_outcomes.<pool>.<crit>` / `quality.tie_comparisons.<pool>.<crit>` (R12, §9.3);
  `quality.judgment` / `quality.bt_fit` / `quality.gate` payloads gain `pool` (R16, §8.1).

v1.8 sequence scoring (`record.kind == "sequence"`; spec 3.4.3 sequence row):

- **Record rendering** switches to the §10.2/§10.3 sequence variant: `[步骤序列]` (the
  transitions rendered as text; a fallback step — `Transition.detail.kind ==
  "extraction_invalid"` — is listed SEPARATELY from an LLM-confirmed `other` by the
  `（摘取兜底）` line suffix, S16, so fallback noise cannot pollute the coherence anchor) +
  `[成员帧摘要]` (bounded per-member `frame_digest`), **NO images** — sequence scoring is
  pure text even in UI modality (the rule-34 vision relaxation, §6.3). transitions and the
  pre-rendered text reach the judging calls via NEW PRIVATE parameters of
  `_judge_once`/`_pointwise_once` (private signatures — not part of the frozen surface);
  the `excerpt` tier payload for sequences = first 200 chars of the member-digest rendering.
- **Rubric**: the stream default is `default:trajectory` (S29, §6.3 rule 36); the rubric is
  consumed by the EXISTING machinery with zero changes. With `extract.enabled = false` the
  steps section is absent and "步骤" degrades to "帧间变化" (M1 warns, §6.3).
- **Gate**: stream mode keeps the existing default of "score only, no filtering" when
  `quality.threshold` is absent — deliberately so (TRM ablation + E2E #6, spec §1.6).

### 7.4 M5 — `labelkit/operators/annotate.py`

```python
@dataclass(frozen=True)                            # [FROZEN HERE]
class RepairContext:
    previous_output: Mapping                       # last annotation object
    critiques_text: str                            # rendered lines "aspect: opinion"
                                                   # (multi-judge: "judge_name/aspect: opinion")


def build_annotate_prompt(record: Record, cfg: ResolvedConfig, schema_text: str,
                          repair: RepairContext | None = None,
                          temperature: float | None = None,
                          label: str | None = None,
                          transitions: tuple[Transition, ...] | None = None) -> PromptBundle:
    """Deterministic template assembly per §10.1. schema_text = SchemaEngine.user_schema_text.
    repair != None appends the repair suffix (§10.5). [FROZEN HERE; label is a v1.7 ADDITIVE
    trailing kwarg (R2): non-None → instruction/examples come from
    cfg.class_views[label].annotate; None = global config — pre-v1.7 call sites unchanged.
    transitions is the SECOND additive trailing-kwarg revision of this frozen signature
    (v1.8, S5 — same R2 construction): non-None → the §10.1 sequence variant renders the
    [动作序列] section from it; None = section omitted / pre-v1.8 behavior byte-identical]"""


async def annotate_record(record: Record, ctx: RunContext,
                          repair: RepairContext | None = None,
                          label: str | None = None,
                          transitions: tuple[Transition, ...] | None = None) -> Annotation:
    """One record's full annotation path incl. self-consistency (skipped when repair != None:
    repair re-annotation is always a single call at profile-default temperature [FROZEN HERE]).
    Raises SchemaViolation / ProviderRetryableError / ProviderFatalError. This is the hook M7
    uses for verify.policy='repair'. [FROZEN HERE; label is a v1.7 ADDITIVE trailing kwarg
    (R2), same semantics as build_annotate_prompt — None = global config. transitions is the
    v1.8 ADDITIVE trailing kwarg (S5): the stage layer passes item.transitions; the M7 repair
    path threads the REBUILT value through after member surgery — None = pre-v1.8 behavior]"""


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

v1.7 label semantics (R2): `label = None` ⇒ globally configured instruction/examples (exactly
the pre-v1.7 behavior); `label` non-None ⇒ both are read from `class_views[label].annotate`.
The stage layer passes `item.classification.label if item.classification else None`. The
`annotate.done` payload gains `label` (classify enabled only, §8.1).

v1.8 sequence annotation (S5/S6/S28; `record.kind == "sequence"` only): the user message
follows the §10.1 sequence variant — `[动作序列]` text (omitted entirely when
`transitions is None`) → per kept keyframe `[关键帧 {i}/{k}·成员 {m}]` text + image →
ALWAYS-CLOSING `[成员帧摘要]` text. **Template invariant: the final part of the user message
is ALWAYS text** — the M8 repair loop concatenates onto `parts[-1].text`, an image-final
message would silently produce "None\n…" and drop the last image (S6); the closing digest
section exists to guarantee this with zero repair-code changes. Keyframe selection: n members
> `annotate.sequence_frames` = k → deterministic uniform downsample
`idx_i = ⌊i·(n−1)/(k−1)⌋, i = 0..k−1` (first/last always kept, strictly increasing, zero
rng; n ≤ k takes all members). Self-consistency and the L2.5 hook paths are UNCHANGED (the
L2.5 callback receives `record=None` for sequence records — documented limitation; a richer
payload is a roadmap candidate).

### 7.5 M6 — `labelkit/operators/generate.py`

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
`<llm>×<style>` with literal `×`; style absent → the string `null` in report; v1.7 — when
classify is enabled the key gains a class prefix, `<class>×<llm>×<style>` (three segments,
same literal `×`); classify disabled keeps the two-segment form byte-identical** — see §9.3].

v1.7 per-class generation (classify enabled, process mode; spec 3.6.2 按类种子池 row):

- **Seeds & thresholds (R19).** `select_seeds` groups the seed pool by
  `item.classification.label`. Per-class threshold chain: global `seed_min_score` → absent:
  the CLASS-effective `quality.threshold` → absent: the median aggregate of that class's own
  seed pool.
- **Lexicographic segment concatenation (R18).** Participating classes (those with seeds)
  occupy consecutive GLOBAL call-index ranges in class-name lexicographic order; per-class
  budget `C_c = ceil(len(seeds_c) × num_per_record_c / num_per_call)`. ONE pass over
  i = 0..C−1 pre-draws the plan: llm by global index exactly as before (round_robin consumes
  zero rng; weighted consumes one `choices` per i); style drawn uniformly from the effective
  styles OF THE CLASS OWNING index i; seed sampling per call in ascending global index order.
  Classify disabled ⇒ a single anonymous segment = the pre-v1.7 behavior, byte-identical.
- **Planner & records (R17).** The internal `CallPlan` gains a `class_name` field; each call
  uses the class-effective `instruction`/`temperature` (`class_views[class_name].generate`);
  `postprocess_samples` returns `list[tuple[Record, str | None]]` (record, class);
  `GenerateStage.run` wraps new records in PipelineItems carrying
  `Classification(label, (label,), "inherited", {})` — the chain's classify stage skips them
  (idempotency, §7.13).
- **generate_only:** the `generate_all` flat path is UNCHANGED (global instruction, no class
  segments); its products are classified normally by the chain's classify stage.

### 7.6 M7 — `labelkit/operators/verify.py`

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

v1.7 label threading (R3): the internal prompt builder `build_verify_prompt` (NOT part of the
frozen surface) gains a `label` parameter — the `[任务指令]` section and `extra_criteria` both
take the class-effective values (`class_views[label].annotate.instruction` /
`class_views[label].verify.extra_criteria`); `_judge_round`/`_reannotate` thread the label
through, and repair re-annotation calls `annotate_record(..., label=...)` (§7.4). The stage
passes `item.classification.label if item.classification else None`; `verify.verdict` payloads
gain `label` (classify enabled only, §8.1).

v1.8 stream branch (sequence envelopes only; spec 3.7 stream branch, S7/S8/S31). The
non-stream path is a REGRESSION ANCHOR — `run_verify_loop` and `VERDICT_SCHEMA` are
byte-unchanged; sequence envelopes are driven by a stage-layer bypass driver:

- **Schema & verdict.** Reviews validate against `defect_verdict_schema()` (§10.7) — three
  top-level keys `{critiques, defects, verdict}`, ALL required (S7). `critiques` flow through
  the existing merge/feed-back chain unchanged; `defects` land in
  `VerificationResult.defects` and `_meta.verification.defects` (§9.1). A `fail` verdict with
  an EMPTY defects array is normalized code-side to one default `label_mismatch` entry (S7).
  Multi-judge: defects = the UNION over judges that voted fail, deterministically
  de-duplicated and sorted by (kind enum order, position, members) (S31).
- **Evidence** (§10.5 sequence variant): `[任务指令]` + `[动作序列]` + `[边界余量]` (the
  frame_digest of the k=2 frames beyond each segment boundary plus each frame's fate:
  noise / adjacent-episode ordinal / none) + `[首帧截图]` + `[末帧截图]` + `[标注结果]`.
- **Two-phase batch-level repair round (S8** — determinism under concurrent gather;
  `policy="repair"` only): ① concurrent review of ALL pending episodes; ② SYNCHRONOUS
  member surgery executed in batch position order (first-come becomes
  deterministic-position-come): shrink — frames named by `defect.members` flip
  `absorbed → dropped_noise` + duck-typed `off_task_member` mark (→ §9.2 rejects); reclaim
  (missing_head/tail/members) — three-level determination: same-`session_id`
  `dropped_noise` frames in the batch noise pool are RE-JUDGED via a direct
  `segment.judge_window` call (§7.14; relation ∈ {continues, advances} ⇒ reclaim,
  `dropped_noise → absorbed`, inserted into `members` by order key) → frames held by an
  ADJACENT episode: mark only, no cross-episode theft → nowhere to be found: the defect
  entry gains a code-side SIBLING key `suspected = "capture_gap"` (`detail` is
  string-typed in the schema, so the annotation cannot nest under it; frames of a
  batch_size-split session get `"session_split"` instead); ③ concurrent seam re-extraction via direct
  `extract.extract_transition` calls (§7.15; 1–2 per surgery, `detail.reseamed = true`);
  ④ synchronous record rebuild (`dataclasses.replace(record, members=...)`; the record
  **id is NOT recomputed**) and transitions rebuild (renumbered so
  `len(transitions) == len(members) − 1` holds); ⑤ concurrent re-annotation via
  `annotate_record(..., transitions=<rebuilt>)` (§7.4); → next-round re-review. Repair
  rounds count against `max_repair_rounds` INCLUDING the first review.
- **Multi fan-out interplay (S8).** Membership-class surgery may execute ONLY on the
  original envelope (first label); cloned siblings downgrade to mark-only. After a repair
  the sibling envelopes' `record` may diverge (shared-by-reference no longer holds for the
  repaired one); same-id output rows are disambiguated by `_meta.stream.repaired` (§7.13).
- **No re-scoring.** Post-repair episodes keep their pre-repair quality scores;
  `_meta.stream.repaired = true` is the marker.
- **Counters (owner M7, §9.3):** `verify.membership_repairs` (surgeries executed),
  `verify.boundary_flags` (mark-only boundary determinations), `verify.defects.<kind>`
  (per defect kind) → `report.stream.verify`. Defect summaries ride the `verify.verdict`
  event payload (content-tiered, §8.1).

### 7.7 M8 — `labelkit/common/runtime/schema_engine.py`

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
`labelkit/common/runtime/schema_engine.py`, imported by stages) — exact JSON in §10.7:

```python
def judgment_schema(criteria_keys: list[str], with_reason: bool) -> dict: ...
def pointwise_schema(criterion_key: str) -> dict: ...
VERDICT_SCHEMA: dict
def samples_schema(num_per_call: int) -> dict: ...
def classification_schema(class_names: list[str], assignment: str,
                          max_labels: int, with_reason: bool) -> dict: ...   # v1.7 (M13), §10.7
def segment_window_schema(frame_count: int, with_reason: bool) -> dict: ...  # v1.8 (M14), §10.7
def action_schema() -> dict: ...                                             # v1.8 (M15), §10.7
def defect_verdict_schema() -> dict: ...                                     # v1.8 (M7 stream),
                                                                             # §10.7
```

The three v1.8 builders are INTERNAL schemas like the rest: no `resolved_at` bucket
counting, no L2.5 hook, keyword set ⊆ the frozen internal-schema keyword set, and NO
`uniqueItems` anywhere (R1 lesson — L0 strict-mode pass-through). The non-stream verify
path keeps using the frozen `VERDICT_SCHEMA`; `defect_verdict_schema()` exists ALONGSIDE it
(two verdict schemas co-exist, S7).

### 7.8 M9 — `labelkit/common/runtime/llm_client.py`

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


@dataclass                                          # v1.6, per-key accumulator [FROZEN HERE]
class KeyUsage:
    calls: int = 0
    rate_limited: int = 0                          # 429s observed on this key
    disabled: bool = False                         # auth-disabled during this run


@dataclass                                          # mutable per-profile accumulator [FROZEN HERE]
class ProfileUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    est_cost_usd: float | None = None              # only when prices configured
    keys: dict[str, KeyUsage] = field(default_factory=dict)
                                                   # v1.6: by env-var name; stays empty for
                                                   # single-key profiles (report omits it then)
    parked_calls: int = 0                          # v1.6: logical calls that parked ≥ once
    parked_ms: int = 0                             # v1.6: total parked wall-clock


@dataclass(frozen=True)                             # [FROZEN HERE]
class ProbeResult:
    profile: str
    ok: bool
    model: str
    latency_ms: int
    error: str | None = None
    key_env: str | None = None                     # v1.6: set by probe_all() on pooled
                                                   # profiles; None on single-key profiles


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
        (embedding profiles). Never raises; failures land in ProbeResult.error.
        Pooled profiles: probes the first key."""

    async def probe_all(self, profile: str) -> list[ProbeResult]:
        """v1.6: one probe per pool key, declaration order, for llm AND embedding profiles
        (each result carries key_env). Single-key profiles → 1-element list equal to
        [await probe(profile)] with key_env=None. Used by `validate --probe` (§7.12);
        cost = pool size probes per referenced profile. Never raises."""

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
capped at 60 s (this jitter RNG is NOT seed-derived — timing only **[FROZEN HERE]**) — v1.6:
this inter-attempt backoff applies to network errors/timeouts/408/409/5xx ONLY; ALL 429 waiting
(with or without `Retry-After`) is expressed as per-key cooldown per the key-pool paragraph
below, which is the single normative statement of 429 timing. At most
`max_retries`; 400/404 → ProviderFatalError immediately, no rotation (request-shape errors are
key-independent).

Key pool (v1.6, spec 3.9.3 密钥池行; single-key profiles are pools of size 1 and keep v1.5
retry accounting, data output and breaker/exit semantics — the 429 WAIT PATH is a v1.6 behavior
revision: `run.max_park_s` bounds Retry-After waits, no-Retry-After cooldown is 300s-capped and
key-scoped, and parking emits WARN + events): request headers are built PER ATTEMPT from the key
selected by least-in-flight, ties broken by declaration order (deterministic, no RNG —
seed-exempt like the retry jitter). A 429 sets a cooldown on the KEY — `Retry-After` honored in
full when present, else full-jitter `random.uniform(0, retry_base_delay_s * 2**c)` capped at
300 s where c = that key's consecutive-429 count (accumulated ACROSS logical calls, reset by a
success ON THAT KEY) — consumes one retry unit, and the next
attempt re-selects immediately: zero wait while another key is live (`llm.key_cooldown` event).
401/403 permanently disables the key for the run (one stderr WARN + `llm.key_disabled`, env-var
NAME only); with live keys remaining, the SAME attempt re-dispatches on the next key consuming
NO retry budget and feeding NOTHING to the breaker (auth failure is deterministic per key, at
most once each); disabling the LAST live key → ProviderFatalError +
`record_provider_result(fatal=True, hard=True)` (immediate open — pools of 1 reproduce the v1.5
first-401 behavior exactly). Quota signaled as 403 is treated as auth (no body sniffing —
spec 1.6 decision). When ALL live keys are cooling, the call PARKS until the earliest cooldown
end (sleeping in ≤ 60 s slices, re-checking the breaker each slice — preserving the v1.5
post-semaphore re-check; emits `llm.pool_parked` + stderr WARN); parking consumes no retry
budget but is capped per logical call by `run.max_park_s` (default 3600; 0 = no parking —
NOTE: 0 on a single-key profile makes every 429 an immediate retry-exhaustion failure) —
overrun → the normal retry-exhaustion path; when the earliest cooldown end provably exceeds the
remaining park budget, fail immediately via the same path (no dead wall-clock). Parking happens
INSIDE the acquired semaphore slot and holds it (throughput is zero anyway while a whole pool
cools); `run.max_park_s` counts park time only, never semaphore queueing. Retry exhaustion
feeds the breaker window (`record_provider_result(fatal=True)`), unchanged (P1-1).

One `asyncio.Semaphore(max_concurrency)` per profile shared by ALL calls (incl. repairs,
verify, probe) — for pools this is the AGGREGATE in-flight cap across all keys of the profile
(v1.6). Image bytes loaded/scaled/encoded per call and released. Metering: accumulate
usage from response; cost = `prompt_tokens/1e6*price_in + completion_tokens/1e6*price_out` when
both prices set; v1.6 adds per-key `KeyUsage` and `parked_calls`/`parked_ms` to `ProfileUsage`
(report emits them only for pools > 1). Breaker interplay: every ProviderFatalError →
`metrics.record_provider_result(fatal=True)` — with `hard=True` for auth only when the failing
key was the profile's last live key (v1.6; absorbed per-key auth failures raise nothing and feed
nothing); retry exhaustion also records `fatal=True`; any success →
`record_provider_result(fatal=False)`; when `metrics.circuit_broken`, `complete`/`embed` raise
`CircuitBreakerTripped` at entry. Trace: `llm.call` after every call (incl. failures) with the
§8.2 payload (+ `key_env` for pools > 1, v1.6); API keys never enter any log path — key
identity is always the env-var NAME.

### 7.9 M10 — `labelkit/orchestration/orchestrator.py`

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
stage in chain order — the SINGLE SUPERSET TUPLE (v1.8)
`_CHAIN_ORDER = ("segment", "dedup", "classify", "extract", "quality", "generate",
"annotate", "verify")` **[FROZEN HERE: the eight-name tuple]** — with segment/extract DEFAULT
OFF, so the effective v1.7 chain dedup → classify → quality → generate → annotate → verify is
a byte-identical degradation (`generate` and `segment` are mutually exclusive per §6.3 rule
29, so the two never co-occupy the chain; `_compose_chain` includes classify in the main,
re-flow AND generate_only chains — items already classified rely on M13's idempotent skip):
build a fresh `RunContext`
(rng derived per §5) and `await stage.run(batch, ctx)`; `generate.run`'s return value is enqueued as
new batch(es) (split at `batch_size`, consecutive `batch_no`, no generate stage); after stages,
`emitter.emit_batch(batch, batch_no)`, then `metrics.flush()` (trace flush follows output flush),
then drop the batch. Emit events `batch.start`/`batch.end` (stage="run"). generate_only: no
ingestor; call `GenerateStage.generate_all(ctx0)` first, batch the records, run the reduced
chain. Stage timing: wall-clock per stage accumulated into `metrics` for `report.timing`
(`metrics.add_stage_time(stage_name, seconds)` **[FROZEN HERE]**). Circuit breaker: catch
`CircuitBreakerTripped` escaping a stage → cancel remaining work, finalize WITH delivery
(v1.6 revision of the frozen rule, spec 3.10.3 熔断交付: `.part` IS fsync'd and renamed —
completed batches are delivered; report gains `run.partial_delivery=true` and the balancing
`counts.unprocessed`, §9.3), `RunSummary.exit_code=4`. Unwritable output (exit 4 at `open()`)
still delivers nothing. SIGINT/SIGTERM: stop taking new
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

v1.7 classify orchestration (spec 3.10.3 分类与扇出 row):

- **Fan-out metering (R9).** `counts.fanout` is measured by M10 in the `_process_batch` chain
  loop as the `len(batch)` delta across the classify stage invocation (same construction as
  deriving `counts.generated` from generate's return value) — M13 never touches `counts.*`
  (§9.3 ownership). `batch.start.size` stays "envelope count at batch ENTRY" (pre-fan-out);
  `batch.end` payload gains `fanout` (R20, §8.1).
- **Breaker residual (R10).** The `counts.unprocessed` balancing residual adds `+ fanout` to
  its source side (scanned + generated + fanout, minus the terminal counts); the `fanout`
  counts key itself appears only when `classify.assignment = "multi"` (§9.3).
- **Dry-run estimate (R11/R28).** `_estimate` gains `classify_calls` — process mode:
  `ingested × max(1, classify.self_consistency)`; generate_only: `<generated records> ×
  max(1, sc)`. quality/annotate/verify estimates use the globally-inherited config; when
  `[class.*]` overrides exist or assignment is "multi", stderr notes "estimated on the global
  config / multi reported as a lower bound (label multiplier 1)".

v1.8 stream orchestration (spec 3.10.3 stream rows; active only when `segment.enabled`):

- **Whole-session batching — next-fit (S21).** M10 consumes `ingestor.sessions()` (§7.1)
  instead of `records()` and packs WHOLE sessions into batches by next-fit (sequential
  packing, exactly ONE open bin): sessions ship in arrival order, a session that no longer
  fits closes the current batch and opens the next. Batch capacity = `run.batch_size`
  FRAMES. A single session longer than `batch_size` is HARD-SPLIT by M10 + ONE stderr WARN
  + a duck-typed `session_split` mark on the split session's frame envelopes (M7's
  missing-frame downgrade evidence and `_meta.stream.session_split`, §9.1). The one pending
  overflow session is the ONLY new cross-batch survivor (released as soon as it is packed —
  it joins the §11 ⑤ closed list).
- **session_id stamping (S4).** M10 stamps `PipelineItem.session_id` on frame envelopes at
  envelope construction (bookkeeping, not business logic); M14 stamps the episode envelopes
  it appends.
- **Episode metering (fanout-isomorphic, R9 construction).** `counts.episodes` = the
  `len(batch)` delta across the SEGMENT stage invocation, metered by M10 — M14 never touches
  `counts.*`.
- **Status tally.** The post-emit tally gains `absorbed`/`dropped_noise`; the `failed`
  fallback formula extends to
  `failed = max(len(batch) − emitted − dropped_dup − dropped_lowq − dropped_verify −
  absorbed − dropped_noise, 0)` — without the new terms, absorbed members would be
  miscounted as failed. `batch.end` payload gains `episodes`/`absorbed`/`dropped_noise`
  (carried only when segment is enabled, R20 form, §8.1); the stderr progress/summary line
  gains NO new keys (fanout precedent — the report carries them).
- **Conservation & interrupted residual (S18).** The full v1.8 invariant is §9.3's
  `emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed +
  bad_input + absorbed = scanned + generated + fanout + episodes`. In stream mode the
  `counts.unprocessed` residual appears on "breaker trip **OR** interrupted" (SIGINT over a
  session buffer strands in-flight records); the residual computation extends both sides
  (`+ episodes` on the source side, `+ absorbed + dropped_noise` on the terminal side).
  Non-stream interrupted runs keep a zero residual and NO `unprocessed` key (regression
  anchor).
- **Dry-run (S22/S23).** `_estimate` gains, unconditionally printed (classify precedent;
  0 when disabled): `segment_calls = Σ ceil((L−1)/(window−1))` over sessions of length
  L ≥ 2 (L = 1 or `strategy="rules"` counts 0) and `extract_calls = Σ (L−1)` reported as an
  UPPER bound; quality/annotate/verify estimates use episodes ≈ sessions as a LOWER bound +
  a stderr note; the batch count is computed EXACTLY by dry-run next-fit packing of the
  session sizes; text-modality line counting and the session dry-run fuse into a single
  read pass (S23, §7.1).

### 7.10 M11 — `labelkit/operators/emitter.py`

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
        report['counts']. deliver=False is used by dry-run only (no .part was opened);
        v1.6: a circuit-break finalize passes deliver=True — completed batches are renamed
        and delivered, the report marking run.partial_delivery=true (spec 3.10.3 熔断交付).
        Report write failure → CLI exit 1 (raise LabelKitError('report write failed')).
        [FROZEN HERE]"""
```

File names: main `run.output`; temp `run.output + ".part"` (same directory); sidecar
`{output_stem}.meta.jsonl` (temp `+ ".part"`); rejects `{output_stem}.rejects.jsonl` (streamed,
no .part — it is an append log like trace **[FROZEN HERE]**); report `{output_stem}.report.json`.
`output_stem` = output path minus final suffix. Line formats: §9.

v1.7: `_assemble_meta` gains the ALWAYS-PRESENT `classification` key (`null` when classify is
disabled, else `{label, labels, source}` — §9.1); the `_meta.scores` block gains `pool`
(classify enabled only); rejects refs lines gain the `label` key (classify enabled only —
the §9.2 closed five-key enumeration becomes six keys, R5). The rejects attribution rule
(`stage`/`reason` from `item.errors[0]`) is UNCHANGED — guaranteed safe because fallback
classification writes no `item.errors` entry (R4, §7.13).

v1.8 (spec 3.11.2 stream rows):

- **Third route.** `status == "absorbed"` goes to NEITHER the main output NOR rejects —
  counted only (the member content lives inside its episode's sequence record). `emit_batch`
  distribution becomes: active → main; absorbed → counted; every other non-active status →
  rejects.
- **Rejects attribution for `dropped_noise`.** `_reject_stage_reason` gains a
  `dropped_noise` branch that reads the duck-typed reason mark left by the flipping stage:
  `("segment", "noise")` | `("segment", "below_min_len")` | `("verify", "off_task_member")`
  (§9.2 — these frames carry no `item.errors` entry, so the `errors[0]` rule cannot serve
  them).
- **`_assemble_meta`** gains the ALWAYS-PRESENT `stream` key (`null` whenever segment is
  disabled), positioned after `source` and before `scores` (chain-order mirror, §9.1); in
  stream mode `_meta.verification` additionally carries the always-present `defects` key
  (`[]` when none — §9.1); non-stream verification blocks do NOT carry the key.
- **`_raw_payload`** (rejects `full` tier) gains a `kind == "sequence"` branch emitting
  `{"kind": "sequence", "member_ids": [...], "member_sources": [...]}` (S25, §9.2) instead
  of the single-record payload shape.

### 7.11 M12 — `labelkit/common/observability/obslog.py`

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
`EV_SCHEMA_REPAIR = "schema.repair"`, `EV_LLM_CALL = "llm.call"`, `EV_ERROR = "error"`,
and (v1.6) `EV_LLM_KEY_COOLDOWN = "llm.key_cooldown"`, `EV_LLM_KEY_DISABLED = "llm.key_disabled"`,
`EV_LLM_POOL_PARKED = "llm.pool_parked"`, and (v1.7)
`EV_CLASSIFY_DECISION = "classify.decision"`, and (v1.8)
`EV_INGEST_DISORDER = "ingest.disorder"`, `EV_SEGMENT_SESSION = "segment.session"`,
`EV_SEGMENT_BOUNDARY = "segment.boundary"`, `EV_EXTRACT_STEP = "extract.step"`.

v1.8 redaction constants (S27, §8.3): `_FREE_TEXT_KEYS` gains `"description"` (LLM-produced
free text — stripped at `none`, carried from `refs`); NEW module constant
`_DATA_KEYS = {"target", "value"}` — INPUT-DATA-DERIVED payload fields (widget text
references, typed-in text), stripped at BOTH `none` and `refs` (the refs tier's
"no input data content" red line), carried from `excerpt`. The channel enumeration
`_TRACE_CHANNELS` (owned by `labelkit/common/config/loader.py`) grows 8 → 10 with
`"segment"`/`"extract"`
(S1: channel = stage name; the `error` event auto-routes by its `stage` field — zero routing
code changes).

### 7.12 CLI — `labelkit/cli/` package

```
labelkit run      --config <config.toml> --project <project.toml>
                  [--input PATH] [--output PATH] [--limit N] [--dry-run] [--strict]
                  [--log-level debug|info|warn|error]
labelkit validate --config <config.toml> --project <project.toml> [--probe]
labelkit rubric   [--show default:text|default:ui|default:trajectory]
```

```python
def main(argv: list[str] | None = None) -> int:    # entry point (pyproject console script)
```

Physical ownership is split without changing the CLI surface: `labelkit/cli/parser.py` owns
argparse definitions and `CliOverrides` conversion; `labelkit/cli/commands.py` owns the `run`,
`validate`, and `rubric` user-facing handlers; `labelkit/cli/main.py` owns the process entry,
exception rendering, and the sole exception-to-exit-code mapping; `labelkit/cli/__init__.py`
preserves the established public imports and `labelkit.cli:main` console-script target.

Wiring order for `run`: CLI parses arguments and calls
`labelkit.orchestration.runtime.execute_run`; that orchestration runtime owns
`labelkit.common.config.load()` →
`setup_logging` → `run_id = secrets.token_hex(6)`,
`run_started_at = datetime.now().astimezone()` → `EventLog` + `MetricsSink` → `LLMClient` →
`SchemaEngine` → `labelkit.orchestration.factory.build_stages()` → `Ingestor` (process mode) →
`Emitter` → `Orchestrator` → `asyncio.run(orch.run())`. The factory owns operator instantiation,
including `DedupIndex`, and the frozen stage order; CLI never imports or constructs those objects.
`labelkit/cli/main.py` then maps the unchanged outcomes: `ConfigError`→2, `InputError`→3, fatal
(`RunSummary.exit_code==4` / unwritable output / auth failure)→4, `--strict` and rejects>0 → 1
(already folded into `RunSummary.exit_code` by M10, §7.9), report write failure → 1, else 0.

`validate`: the command handler calls `labelkit.orchestration.runtime.validate_project`; with
`--probe`, it calls `probe_referenced_profiles`, which uses
`labelkit.orchestration.profile_usage.referenced_profiles` and `LLMClient.probe_all` on every
referenced profile (v1.6 — one line per key for pooled profiles; single-key output format
unchanged). Any probe failure does not change the exit code unless config itself is invalid
**[FROZEN HERE]**. `rubric`: `labelkit/cli/commands.py` lists available names when no flag is
given; `--show <name>` prints the packaged TOML verbatim (`_RUBRIC_FILES` / argparse choices
include `default:trajectory` → `default_trajectory.toml`, v1.8).

v1.8: `labelkit.orchestration.factory.build_stages` constructs `SegmentStage` and `ExtractStage`
per their switches at their `_CHAIN_ORDER` slots (§7.9).
`labelkit.orchestration.profile_usage.referenced_profiles()` (the `validate --probe` set) gains
`segment.llm` ONLY when `segment.enabled` and `segment.strategy ∈ {llm, hybrid}`, and
`extract.llm` whenever `extract.enabled` (S30, §6.3 rule 33 — the same conditions govern all four
reference sets).

### 7.13 M13 — `labelkit/operators/classify.py` (v1.7)

(New module, spec 3.13. Numbered AFTER the pre-existing 7.12 CLI section so every
frozen §7.x anchor in code and docs stays valid; chain position is dedup → **classify** →
quality, §2.)

Responsibilities: closed-set LLM classification of batch items with `status == "active"` and
`classification is None` against the user's class table (single/multi assignment, optional
self-consistency voting); result written to `item.classification`; multi assignment fans
sibling envelopes out to the batch tail per label. Boundaries: never drops records; does not
define class semantics; does not annotate; does not change the chain structure (fan-out only
changes envelope cardinality within the batch). Depends on M1, M8, M9 only.

```python
class ClassifyStage(Stage):
    name = "classify"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
        # returns the SAME list object it received (multi may tail-append, contract ②a §5)


def build_classify_prompt(record: Record, cfg: ResolvedConfig,
                          with_reason: bool) -> PromptBundle:
    """Deterministic assembly of the §10.8 template (class table in declaration order;
    per-class examples; text/UI record parts)."""


async def classify_record(record: Record, ctx: RunContext) -> Classification:
    """One record's full classification path incl. self-consistency voting and
    normalization; the on_error policy is applied by the stage layer."""
```

Normative behavior:

- **Call & validation.** One call per record (× n under self-consistency), through
  `SchemaEngine.complete_validated(schema=classification_schema(...))` (§10.7) — an INTERNAL
  schema: no `resolved_at` bucket counting, no L2.5 hook. Temperature 0; sc samples use
  `classify.sc_temperature`. `reason` is requested iff `trace.enabled` and `"classify"` in
  `trace.channels` (R29). Record-level concurrency via `asyncio.gather` bounded by the
  profile semaphore (skeleton mirrors M5 — own voting code, NOT `annotate._majority_vote`,
  R26).
- **Normalization (after M8, deterministic, fixed order).** ① map labels onto class-table
  declaration order and DE-DUPLICATE; ② the fallback class co-occurring with concrete
  classes ⇒ drop the fallback class (a pure-fallback result is kept). Normalization only
  narrows an already-validated set (schema-side `uniqueItems` deliberately absent, R1/§10.7).
- **sc voting.** `self_consistency = n` (0 = off; ≥ 3 odd): n independent samples; a
  SchemaViolation sample abstains, the denominator stays n. single: majority vote, no
  majority ⇒ fallback class; multi: keep each label appearing in > n/2 sample sets, none
  survive ⇒ fallback class. `detail.sc = {"n", "agreement_ratio"}` (single = winning-class
  vote share; multi = lowest vote share among kept labels).
- **Failure & fallback — two paths (R4).** M8 repair exhausted: `on_error="fallback"`
  (default) ⇒ fallback class with `source="fallback"`, evidence recorded in
  `Classification.detail` (kind + message) — **never in `item.errors`** (keeps §9.2 rejects
  attribution via `errors[0]` unpolluted) — plus an `error` trace event
  (kind=`classification_invalid`) and the `classify.fallback` counter;
  `on_error="fail"` ⇒ `status="failed"`, StageError appended to `item.errors` → rejects.
- **Multi fan-out.** Normalized hit set of k ≥ 2: the original envelope takes the FIRST
  label (declaration order); each remaining label clones one sibling `PipelineItem`
  appended IN PLACE to the tail of the passed-in batch list. Clones share `record` and
  `dedup` BY REFERENCE (sibling rows' `_meta.dedup` stay consistent) and inherit
  `session_id` (v1.8: sibling episodes stay addressable for the M7
  boundary-margin/neighborhood queries); `classification`
  swaps `label` (`labels` = the same full set); `status="active"`;
  scores/annotation/verification/errors are fresh default containers. Append order =
  (original element's batch position → label declaration order), byte-reproducible. Return
  value = the same list object passed in.
- **Idempotency.** Items with `classification is not None` are skipped (covers generated
  records' `"inherited"` Classification on re-flow, §7.5, and any re-entry).
- **Events & counters (ownership).** One `classify.decision` per record (payload: `label`,
  `labels` — multi carries the full set, `source`[, `reason`][, `sc`], §8.1; trace-only,
  R29). Counters OWNED BY M13: `classify.classes.<name>` (counted per label),
  `classify.fallback`, `classify.failures`, `classify.multi_label_records`. `counts.fanout`
  is counted by M10 (len-delta metering, R9/§7.9) — M13 never increments `counts.*`.
- **v1.8 sequence branch** (`record.kind == "sequence"`; spec 3.13.3 sequence row —
  zero-crash guarantee for episodes): the current-record user message follows the §10.8
  sequence variant — `[待分类数据·序列]` episode digest (per-member `frame_digest` in member
  order, TOTAL capped at `input.ui_tree_max_chars` with first/last members always kept and
  whole middle entries truncated + an `…(truncated N members)` marker) + the FIRST member's
  screenshot (UI modality; classify stays in the rule-34 vision set).
- **v1.8 multi × episode semantics (S9).** Fan-out clones always carry
  `transitions = None` (extract runs AFTER classify in the chain — each sibling extracts
  independently under its own label's effective `[class.<label>.extract]` instruction;
  ×k extract cost is accepted, per-label whitelist promise honored). SHARED-RECORD BOUNDARY:
  the v1.7 "clones share `record` by reference" invariant holds only until M7 member
  surgery — a repaired sibling's `record` diverges (same `_meta.id` output rows may then
  carry different `member_ids`), disambiguated by `_meta.stream.repaired` (§7.6/§9.1).

### 7.14 M14 — `labelkit/operators/segment.py` (v1.8)

(New module, spec 3.14 / `spec/314-m14-segment.md`. Numbered AFTER §7.13 so every frozen
§7.x anchor stays valid; chain position is the HEAD of the chain — before dedup, §7.9/§2.)

Responsibilities: refine the batch's candidate sessions into episodes — regroup active
frame envelopes (`kind == "single"`) by `session_id` (batch position order = session order,
guaranteed by M10's whole-session packing, §7.9); optional LLM sliding-window boundary
verdicts + per-frame noise marking (§10.9); flip members to `absorbed` / noise frames to
`dropped_noise`, assemble sequence Records (member order-key ascending) and tail-append
episode envelopes per contract ②b (§5). Boundaries: no ordering/sessionization (M2, §7.1);
no dedup (M3); no action inference (M15); no task labels (M5); no chain-structure changes.
Depends on M1, M8, M9 only. Envelopes with `kind == "sequence"` never enter its processing
face — naturally idempotent.

```python
class SegmentStage(Stage):
    name = "segment"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
        # returns the SAME list object it received (contract ②b tail-append, §5)


def build_segment_prompt(frames: Sequence[Record], diffs: Sequence[Mapping | None],
                         cfg: ResolvedConfig, with_reason: bool) -> PromptBundle:
    """Deterministic assembly of the §10.9 template; frame digests and adjacent-frame
    diffs are pre-assembled code-side (frame_digest/tree_diff, §3)."""


async def judge_window(frames: Sequence[Record], ctx: RunContext) -> list[str]:
    """One window, one call — through complete_validated(schema=
    segment_window_schema(len(frames), with_reason), §10.7). Post-validation is INSIDE this
    function: build the index table FIRST-WINS (duplicate index keeps the first occurrence),
    absent frames default to "continues" (conservative-neutral — the quality
    "absent criterion → tie" precedent); returns the per-frame relation list ALIGNED with
    `frames`. Emits one segment.boundary event per window (§8.1). PUBLIC DIRECT-CALL
    SURFACE: M7's member-reclaim re-judgment calls this function directly (§7.6) — the
    sanctioned import exception registered in the ground rules."""
```

Normative behavior (spec 3.14.4):

- **Strategy** (`segment.strategy`): `"rules"` — candidate sessions become episodes as-is,
  zero LLM (noise_filter/min_len ineffective); `"llm"`/`"hybrid"` (default hybrid) —
  sliding-window refinement, identical behavior inside M14 (rule-layer sessionization is
  always on in M2; "hybrid" names the composition). Window length `segment.window` (≥ 2);
  step = window − 1 (1-frame overlap; the seam frame's WHOLE verdict belongs to the LATER
  window — unconditional overwrite during stitching); `len(session) == 1` degrades to rules
  (zero LLM).
- **Calls & stitching.** One call per window; ALL windows across ALL sessions of the batch
  join ONE `asyncio.gather` (profile semaphore bound); stitching is a synchronous pass after
  all verdicts arrive, positioned by window index — schedule-independent; zero rng.
- **Deductive mapping (code-side lookup — the LLM never answers the boundary question):**
  `continues`/`advances` → non-boundary; `returns_to_entry`/`context_switch` → boundary
  (THAT frame is the first frame of a new segment); `interruption` → noise. The session's
  FIRST frame is always a segment head (rel[0]'s boundary value is ignored; noise[0] still
  applies).
- **Segment assembly (deterministic, per session):** ① noise removal (`noise_filter=true`:
  `interruption` frames → `dropped_noise`, duck-typed reason `"noise"`, incl. frame 0);
  ② split remaining frames at boundary frames; ③ `min_len` check — applies ONLY to the
  segments cut in step ② (S11): a segment shorter than `segment.min_len` flips ALL its
  frames to `dropped_noise` with reason `"below_min_len"` (≠ "noise"; independent counter,
  §9.3); rule-layer lone-frame/short sessions never pass through min_len; ④ per segment:
  members order-key ascending → members `absorbed` → build the sequence Record (§3 id rule;
  ref inherits the first member, S24) → tail-append the episode envelope (`active`,
  `kind="sequence"`) + stamp `session_id`.
- **Failure (`segmentation_invalid`, §4):** a window whose M8 repair budget is exhausted —
  `on_error="keep"` (default): the session abandons ALL window verdicts and becomes ONE
  whole episode (zero noise removal, zero splitting); evidence triple =
  `_meta.stream.degraded = {kind: "segmentation_invalid", windows_failed: k}` + `error`
  event + `segment.failures` counter, **never `item.errors`** (S26 — rejects attribution
  reads `errors[0]`, §9.2); `on_error="fail"`: all session members `failed` → rejects.
- **Digest-poverty guard (S12).** A frame whose `frame_digest` judges poor (zero visible
  text nodes / digest < 8 chars) counts `digest_poor_frames` (§9.3) + at most ONE stderr
  WARN per run pointing at `segment.use_vision`.
- **Events:** `segment.boundary` per window (§8.1). `segment.session` is emitted by M2's
  assembler (§7.1), not by this module. Counter owned by M14: `segment.failures`;
  `below_min_len`/`digest_poor_frames` report fields are M14-owned (§9.3);
  `counts.episodes`/`absorbed`/`dropped_noise` are M10's (§7.9).

### 7.15 M15 — `labelkit/operators/extract.py` (v1.8)

(New module, spec 3.15 / `spec/315-m15-extract.md`. Chain position: after classify, before
quality, §7.9 — labels are in place so `[class.<label>.extract]` per-class instructions
apply.)

Responsibilities: for every active sequence envelope (episode), infer one structured action
per adjacent member pair ⟨s_i, s_{i+1}⟩ via LLM (internal schema §10.7) and write
`item.transitions`; **transition count = member count − 1**. UI-modality sequences only
(§6.3 rule 30). Boundaries: no re-segmentation (M14 upstream — the member set is given
input); no user-schema fields (the step sequence is tool-internal structure — it reaches
`_meta.stream.steps` and downstream prompts; user-schema output belongs to M5); no record
elimination (the default failure path is fallback evidence, not dropping); no scoring, no
review. Depends on M1, M8, M9 only.

```python
class ExtractStage(Stage):
    name = "extract"
    def __init__(self, cfg: ResolvedConfig): ...
    async def run(self, batch: list[PipelineItem], ctx: RunContext) -> list[PipelineItem]: ...
        # returns the SAME list object (no additions, no removals; §5 contract ②)


def build_extract_prompt(prev: Record, curr: Record, cfg: ResolvedConfig,
                         label: str | None) -> PromptBundle:
    """Deterministic assembly of the §10.10 template; label non-None → instruction takes
    class_views[label].extract's effective value (§6.3 rule 35)."""


async def extract_transition(prev: Record, curr: Record, index: int,
                             ctx: RunContext, label: str | None = None) -> Transition:
    """One transition, one call — through complete_validated(schema=action_schema(),
    §10.7); repair exhaustion follows extract.on_error (fallback Transition / raise).
    PUBLIC DIRECT-CALL SURFACE: M7's post-surgery seam re-extraction calls this function
    directly (1–2 calls per surgery; rebuilt Transitions carry detail.reseamed=true and
    renumbered index so len(transitions) == len(members) − 1 stays true, §7.6) — the
    sanctioned import exception registered in the ground rules."""
```

Normative behavior (spec 3.15.4):

- **Selection & idempotency.** Processes envelopes with `status == "active"`,
  `record.kind == "sequence"` AND `transitions is None`; `transitions is not None` skips
  (any re-entry costs zero calls). The M7 repair path never re-runs this stage — it uses
  the `extract_transition` direct call.
- **Output invariant.** `item.transitions` length == `len(record.members) − 1`, ascending
  by pair ordinal; a single failed transition never breaks the invariant (fallback
  placeholder, §4 extraction_invalid). Batch cardinality unchanged.
- **Concurrency.** ALL transitions across ALL episodes of the batch join ONE
  `asyncio.gather` (M4 pairwise phase-2 skeleton); results written back by (episode batch
  position, pair ordinal) — schedule-independent, zero rng. Temperature 0. One request
  carries exactly 2 images.
- **Multi fan-out (S9).** Each sibling extracts independently under its own label
  (per-label `instruction`); `transitions` is per-envelope self-contained; dry-run reports
  the ×1 lower bound + stderr note (R28 convention, §7.9).
- **Fallback semantics (S16).** On repair exhaustion with `on_error="fallback"` (default):
  the step records the code-side fallback
  `action = {"action_type": "other", "target": None, "value": None, "description": ""}` +
  `detail = {kind: "extraction_invalid", message}`; the episode stays alive, later
  transitions extract normally; **never `item.errors`**; the evidence keeps fallback steps
  distinguishable from LLM-confirmed `other` downstream (detail.kind presence).
  `on_error="fail"`: episode `failed` → rejects (+ `extract.failures`).
- **Events & counters (owner M15, §9.3):** one `extract.step` per transition incl. fallback
  steps (§8.1); `extract.transitions` (total incl. fallback), `extract.fallback_steps`,
  `extract.failures`, `extract.by_type.<action_type>` (per-type distribution — systematic
  degradation observable, S14; feeds `include_diff` A/B).

---

## 8. Observability contract (M12 + ch.7)

### 8.1 Event catalog (stable contract, `trace_schema_version = 1`, additive-only)

| Event `ev` | Channel / stderr level | Emitted by / when | `record_ids` | payload fields |
|---|---|---|---|---|
| `run.start` | always / info | M10, after M1 passes, before first batch; trace header line | () | `tool_version`, `config_digest`, `project_digest`, `trace_schema_version` (=1, only here) |
| `run.end` | always / info | M10 after finalize; last trace line | () | `counts` (report-shaped summary), `exit_code` |
| `batch.start` | always / debug | M10 when PipelineItem[] ready | () | `size` |
| `batch.end` | always / info | M10 after batch emit + release | () | `active`, `dropped_dup`, `dropped_lowq`, `dropped_verify`, `failed`, `duration_ms`[, `fanout` — v1.7, classify enabled only (R20)][, `episodes`, `absorbed`, `dropped_noise` — v1.8, segment enabled only (same R20 form)] |
| `ingest.bad_line` | ingest / warn | M2 bad line skipped | () | `file`, `line_no`, `reason` |
| `ingest.missing_pair` | ingest / warn | M2 missing pair skipped | () | `index`, `present` ("tree"\|"image"), `file` |
| `ingest.index_conflict` | ingest / warn (error if policy=fail) | M2 index conflict | () | `index`, `files` (list) |
| `ingest.disorder` | ingest / — (trace-only, no per-event stderr mirror) (v1.8) | M2 when the streaming monotonicity check rejects a record (out-of-order or timestamp parse failure, `stream.on_disorder`, §7.1); skip policy: one event PER RECORD, plus ONE data-free stderr WARN per run logged by M2 itself (the reason embeds timestamp/cursor values and never reaches stderr — spec §7.1 ①); fail policy terminates via InputError (exit 3) | () | `file`, `line_no` (text) \| `index` (UI), `reason` ("乱序" \| "时间戳解析失败"-class wording, carries the offending values — trace channel only) |
| `segment.session` | segment / — (trace-only, no stderr mirror) (v1.8) | M2's session assembler closing a candidate session (§7.1; `--limit` truncation treated as EOF flushes the tail session, S17) — emitted by M2 but prefix-routed to the segment channel (S1) | () | `session_id`, `first` / `last` (first/last order keys), `len`, `cause` ("gap"\|"key"\|"max_len"\|"max_span"\|"eof"\|"limit") |
| `segment.boundary` | segment / — (trace-only, no stderr mirror) (v1.8) | M14 per sliding window once the verdict passes M8 (§7.14); member provenance lives in the payload | () | `session_id`, `window` (= [s, e] frame-ordinal span), `member_ids`, `relations`[]{`index`, `relation` (five-value closed vocabulary, §10.9)}, `model`[, `reason`†] |
| `dedup.duplicate` | dedup / — | M3 duplicate verdict | (dup id,) | `kind`, `cluster_key`, `kept_id`, plus exactly one of `jaccard` (near_text) / `hamming` (near_image) / `cosine` (near_semantic); exact dups carry none |
| `classify.decision` | classify / — (trace-only, R29) | M13 per record once the classification is final (v1.7) | (id,) | `label`, `labels` (multi: full hit set), `source` ("llm"\|"fallback"\|"inherited")[, `reason`][, `sc` {n, agreement_ratio}] |
| `extract.step` | extract / — (trace-only, no stderr mirror) (v1.8) | M15 per adjacent-pair transition finalized, incl. fallback steps (§7.15) | (s_i id, s_{i+1} id) | `episode_id`, `index`, `action_type`, `description`‡, `target`§, `value`§ |
| `quality.judgment` | quality / — | M4 per pairwise judgment after M8 pass | (first-sampled record, second-sampled record) — SAMPLING order, NOT the presented A/B order; the A/B mapping lives in `payload.order` (spec 7.2/7.3) | `order` ({"A": id, "B": id} presented), `model`, `judgments`[]{`criterion`, `winner` "A"\|"B"\|"tie"[, `reason`]}[, `judge`][, `pool` — v1.7, classify enabled only (R16)] |
| `quality.pointwise` | quality / — | M4 per record per criterion | (id,) | `criterion`, `score` (raw 0–5), `reason` |
| `quality.bt_fit` | quality / — | M4 per batch per criterion (v1.7: per pool per criterion) | () | `criterion`, `iterations`, `converged`, `comparisons`[, `pool` — v1.7, classify enabled only (R16)] |
| `quality.gate` | quality / — | M4 gate decision per record (threshold set or top_ratio) | (id,) | `aggregate`, `decision` ("keep"\|"drop")[, `threshold`][, `selection`, `top_ratio`, `rank`][, `pool` — v1.7, classify enabled only (R16)] |
| `annotate.done` | annotate / — | M5 after M8 pass | (id,) | `attempts`[, `sc` {n, agreement_ratio}][, `label` — v1.7, classify enabled only (R5)] |
| `verify.verdict` | verify / — | M7 per round (per judge when judges set) | (id,) | `verdict`, `round`, `critiques`[]{`aspect`, `opinion`}[, `judge`][, `label` — v1.7, classify enabled only (R5)] |
| `schema.repair` | schema / — | M8 any non-clean resolution | (record ids if known) | `resolved_at` ("l1"\|"l3_1"\|"l3_2"\|"rejected"), `violations` (JSON-Pointer + violated keyword summary, NO data values)[, `l1_lossy`=true — v1.5, only on a suspected content-dropping L1 repair] |
| `llm.call` | llm / debug (summary always) | M9 after every call incl. failures | () | `profile`, `gen_ai.request.model`, `latency_ms`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `retries`, `status` ("ok"\|"retryable_exhausted"\|"fatal")[, `operation`="embedding"][, `key_env` — env-var name of the key used by the LAST attempt (success or failure); absent on zero-attempt calls; pooled profiles (>1 key) only, v1.6][, `gen_ai.input.messages`, `gen_ai.output.messages` — content="full" + llm channel only] |
| `llm.key_cooldown` | llm / — | M9 when a key enters 429 cooldown (v1.6, spec 3.9.3); fires for ANY pool size incl. 1 | () | `profile`, `key_env`, `cooldown_s`, `retry_after` (bool: duration came from the Retry-After header) |
| `llm.key_disabled` | llm / warn | M9 when a key is auth-disabled — at most once per key per run; any pool size incl. 1 (single-key: precedes the hard trip) (v1.6) | () | `profile`, `key_env`, `status_code` |
| `llm.pool_parked` | llm / warn | M9 when a call starts parking — all live keys cooling; any pool size incl. 1 (v1.6) | () | `profile`, `wait_s`, `live_keys` |
| `error` | channel of producing stage / warn (record-level) · error (run-level) | On StageError construction | per case | `stage`, `kind` (§7.6 codes), `message`, `retryable`[, `label` — v1.7, classify enabled only (R5)] |

`reason` present only when `quality.judgment_reasons` is effective (`classify.decision`: only
when requested per R29, §7.13; † `segment.boundary`: the same construction — requested iff
`trace.enabled` and `"segment"` in `trace.channels`, = the schema's `with_reason`, §7.14).
‡/§ are `extract.step` content-tier marks (S27, §8.3): `description` carried from `"refs"`,
`target`/`value` carried from `"excerpt"`. `run.*`/`batch.*` bypass the
`trace.channels` filter and use `stage="run"`, `batch_no` = current batch (0 for run.*).
Channel enumeration (v1.8): 8 → 10 — `trace.channels` accepts
ingest|segment|dedup|classify|extract|quality|annotate|verify|schema|llm (channel = stage
name, S1); the `error` event keeps routing by its `stage` field, so segment/extract stage
errors reach their channels with zero routing changes.

### 8.2 Trace line format

One JSON object per line, UTF-8, exactly the seven fields
`ts, run_id, batch_no, stage, ev, record_ids, payload` (test-asserted). `ts` ISO8601 with
milliseconds and timezone offset, e.g. `2026-07-02T09:31:04.482+08:00`.

### 8.3 `trace.content` redaction tiers

| Tier | Payload content |
|---|---|
| `"none"` | ids, enums, numbers only; NO LLM-produced free text (`reason`/`critiques`/`violations`/`description` omitted) |
| `"refs"` (default) | + LLM-produced text (reason / critiques / violations / description), NO input data content |
| `"excerpt"` | + `excerpt` field on `quality.judgment` / `quality.pointwise` / `annotate.done` / `verify.verdict`: `{record_id: first 200 chars}` (text: `Record.text`; UI: `UITree.serialize()` output; never images); + the `_DATA_KEYS` fields (v1.8, below) |
| `"full"` | + `gen_ai.input.messages` / `gen_ai.output.messages` on `llm.call` (requires "llm" in channels) |

v1.8 (S27): two redaction key sets in `labelkit/common/observability/obslog.py` (§7.11) —
`_FREE_TEXT_KEYS` gains
`"defects"` (the verify.verdict stream defect table carries LLM free text in `detail`;
dropped whole-key at tier "none", same level as critiques) and
`"description"` (LLM-produced text: stripped at `none`, carried from `refs`, same tier as
reason/critiques); NEW `_DATA_KEYS = {"target", "value"}` — these `extract.step` payload
fields are INPUT-DATA-DERIVED (widget text references, typed-in text) and are stripped at
BOTH `none` and `refs` (preserving the refs tier's "no input data content" red line),
carried from `excerpt`. Per-event tier quick reference: `extract.step` none =
{episode_id, index, action_type}, refs = + description, excerpt = + target/value;
`segment.boundary` none = structural fields (session_id/window/member_ids/per-frame
relations/model), refs = + reason (the key is already in `_FREE_TEXT_KEYS`). The three
v1.8 events (`segment.session`/`segment.boundary`/`extract.step`) have NO stderr mirror
(trace-only, §8.1).

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
  // v1.8 — ALWAYS-PRESENT key (null whenever segment is disabled); key position AFTER
  // "source" and BEFORE "scores" — chain-order mirror (spec §6.3):
  "stream": null | {
      "episode_id": "<sequence record id>",
      "session_id": "<session id>",
      "order_span": [<first order key>, <last order key>],
      "member_count": <int>,
      "member_ids": ["<member record id>", ...],
      "member_sources": [{"file": ..., "pair_index"|"line_no": ...}, ...],
      "session_split": false,      // the owning session was hard-split at batch_size
                                   // (S21; M7's missing-frame downgrade evidence)
      "repaired": false,           // verify defect repair rewrote the member set
                                   // (§7.6; disambiguates same-id sibling rows under
                                   // multi fan-out, §7.13)
      "degraded": null | {"kind": "segmentation_invalid", "windows_failed": <int>},
                                   // segment.on_error="keep" evidence (S26)
      "steps": null | [{"index": <int>, "action_type": "<enum>", "target": <str|null>,
                        "value": <str|null>, "description": "<str>"}, ...]
                                   // extract disabled → always null; enabled = the
                                   // transitions rendered verbatim, step by step (§7.15)
  },
  "scores": null | {"<criterion>": <float|null>, ..., "__aggregate__": <float|null>,
                    "mode": "pairwise_bt"|"pointwise", "batch_no": <int>
                    [, "pool": "<label>"]},        // v1.7: pool key ONLY when classify enabled
  "dedup": null | {"kind": "unique"},
  // v1.7 — ALWAYS-PRESENT key (null when classify is disabled, like other disabled stages);
  // key position between "dedup" and "annotation" per the spec §6.3 example (chain order):
  "classification": null | {"label": "<class>", "labels": ["<class>", ...],
                            "source": "llm"|"fallback"|"inherited"},
  "annotation": null | {"model": "<model>", "attempts": <int>
                        [, "sc": {"n": <int>, "agreement_ratio": <float>}]},
  "verification": null | {"verdict": "pass"|"fail", "rounds": <int>
                          [, "defects": [{"kind": ..., "members": ..., "position": ...,
                                          "detail": ...}, ...]]}
                          // v1.8: "defects" is carried in STREAM MODE ONLY and is then
                          // ALWAYS present ([] when no defects, spec §6.3); non-stream
                          // verification blocks never carry the key
}
```

`_meta.run.rubric` = the configured selector (`"default:text"`/`"default:ui"`/
`"default:trajectory"` — v1.8, incl. as the resolved product of an empty selector under
stream, S29) or, for inline, the rubric's `name` **[FROZEN HERE]**. A disabled stage →
`null` for its key. v1.7: under
multi fan-out the main-output line key is (`_meta.id`, `classification.label`) — sibling rows
share the record id (spec §6.3). v1.8: `stream` is the SOLE new always-present key — with
segment disabled every v1.7-era line differs from v1.7 output ONLY by `"stream": null`
(spec §6.3; the four pre-existing example projects re-verify this byte-diff).

### 9.2 Rejects channel (spec 3.11.2)

`{output_stem}.rejects.jsonl`. `rejects="refs"` (default) — one line per rejected item, no data
content whatsoever (no passthrough fields either). Per spec 3.11.2 the refs line carries
**exactly** the five `_meta` keys `{id, source, stage, reason, errors}` (a closed enumeration:
每行仅 …) — v1.7 revision (R5): **six** keys when classify is enabled, adding `label` (the
envelope's routing label; disambiguates fanned-out siblings that share a record id; classify
disabled keeps the five-key form byte-identical) — no status-specific evidence keys.
Duplicate-cluster / quality-gate / verdict
evidence is auditable via the trace events instead (`dedup.duplicate`, `quality.gate`,
`verify.verdict`, §8.1):

```jsonc
{"_meta": {
  "id": "<record id>",
  "source": {"file": ..., "line_no"/"pair_index": ... (same convention as §9.1),
             "generated_from": [...] [, "generator": {...}]},   // NO "fields"
  "stage": "<stage that rejected>",         // dedup | quality | verify | annotate | emitter ...
  "reason": "<see table>",
  "errors": [ "<pointer>: <violation>", ... ],  // always present; [] when item.errors is empty
                                                // [FROZEN HERE: [] rather than omission]
  "label": "<class>"                        // v1.7: ONLY when classify enabled (R5);
                                            // null when the item was never classified
}}
```

`reason` values **[FROZEN HERE]**: `dropped_dup` → the DedupInfo kind (`"exact"`,
`"near_text"`, `"near_image"`, `"near_both"`, `"near_semantic"`); `dropped_lowq` →
`"below_threshold"` or `"top_ratio"`; `dropped_verify` → `"verify_fail"`; `failed` → the first
`StageError.kind`; v1.8 — `dropped_noise` → by duck-typed mark (§7.10), adding exactly
THREE new (stage, reason) combinations: (`"segment"`, `"noise"`) — LLM-judged noise frame,
(`"segment"`, `"below_min_len"`) — short-segment frame (independent of noise, S11),
(`"verify"`, `"off_task_member"`) — repair-shrunk member frame (S31). `absorbed` items never
reach this file (third route, §7.10). `--strict` note: stream-mode noise frames are EXPECTED
engineering rejects — `--strict` will exit 1 on them (spec 3.11.2/manual). `rejects="full"`
adds `"record"` — text: `Record.raw`; UI:
`{"ui_tree": serialize(), "image_path": str}`; v1.8 sequence records:
`{"kind": "sequence", "member_ids": [...], "member_sources": [...]}` (S25 — the frozen
single-record payload shapes stay for `kind="single"`) **[FROZEN HERE, v1.8-revised]** —
and `"raw_last_output"` (for schema_violation ONLY: classification_invalid /
segmentation_invalid / extraction_invalid failure lines carry no raw output — a known,
accepted gap since v1.7, spec §7 已知锐边). `rejects="none"`: no file.

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
  // run block: + "partial_delivery": true (v1.6, present ONLY on a breaker-trip delivery,
  //            always alongside circuit_broken=true);
  // counts: + "unprocessed" (v1.6, present ONLY on a breaker-trip run — the balancing residual,
  //         see the invariant note below);
// pairwise quality additionally carries "per_criterion_tie_rate" (v1.5, judged comparisons only)
  "schema_engine": {"resolved_at": {"l0_or_clean": 0, "l1": 0, "l3_1": 0, "l3_2": 0,
                                    "rejected": 0}},
  // optional blocks:
  // "annotate": {"sc_disagreements": 0}                       (self-consistency enabled)
  // "generate": {"buckets": {"<llm>×<style|null>": {"calls": 0, "produced": 0,
  //                                                 "survived_dedup": 0}}} (generate enabled)
  // v1.7, ONLY when classify.enabled:
  // "classify": {"assignment": "single"|"multi", "classes": {"<name>": 0, ...},
  //              "fallback_count": 0, "failures": 0
  //              [, "multi_label_records": 0]}                (multi only)
  // counts: + "fanout" (multi only — feeds the invariant below, R9/R10/R20);
  // quality: + "by_class": {"<pool>": {"mode": ..., "rounds": ..., "aggregate_histogram":
  //              {...}, "per_criterion_mean": {...}, "per_criterion_tie_rate": {...}}}
  //   — top-level quality.mode/rounds keep the globally-inherited base values; by_class
  //     carries each pool's EFFECTIVE mode/rounds; tie_rate emission is gated on "at least
  //     one pairwise pool exists" instead of the global mode (R12/R14);
  // generate.buckets keys gain the class prefix "<class>×<llm>×<style|null>" (§7.5)
  // v1.8, ONLY when segment.enabled:
  // counts: + "episodes" (segment-stage len delta, M10-metered — fanout-isomorphic, §7.9),
  //         + "absorbed", + "dropped_noise" (post-emit status tallies, §7.9);
  //         "unprocessed" appearance condition widens in stream mode to
  //         "breaker trip OR interrupted" (S18 — see the invariant note below);
  // "stream" block (placed after "counts", spec §6.4):
  // "stream": {"sessions": 0, "episodes": 0, "mean_episode_len": 0.0, "absorbed": 0,
  //            "dropped_noise": 0, "below_min_len": 0, "digest_poor_frames": 0,
  //            "segment_failures": 0
  //   [, "extract": {"transitions": 0, "fallback_steps": 0, "failures": 0,
  //                  "by_type": {"<action_type>": 0, ...}}]      (extract enabled only)
  //   [, "verify": {"membership_repairs": 0, "boundary_flags": 0,
  //                 "defects": {"<kind>": 0, ...}}]}             (verify enabled only)
  //   — stream.sessions data source = IngestReport.sessions (M2 owner, §7.1);
  //     IngestReport.disorder is a SUB-COUNT of counts.bad_input (audited via the
  //     ingest.disorder events; NO separate report key — spec §6.4); below_min_len is
  //     counted independently of noise (S11); digest_poor_frames per the §3 poverty
  //     judgment; extract.by_type = per-action-type distribution (S14);
  //     verify sub-block per §7.6 (S31)
  "trace": {"enabled": true, "path": "...", "events": 0, "dropped_events": 0},
  "llm_usage": {"<profile>": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                              "est_cost_usd": 0.0, "retries": 0
                              /* v1.6: + "keys": {"<api_key_env name>": {"calls": 0,
                                            "rate_limited": 0, "disabled": false}}
                                 (pools >1 only; ONE entry per pool member — unused
                                 keys appear zeroed); + "parked_calls": 0, "parked_ms": 0
                                 (pools >1, or whenever nonzero — single-key parking
                                 must leave report evidence) */}, ...},
  "timing": {"wall_s": 0, "per_stage_s": {"dedup": 0, "quality": 0, "annotate": 0,
                                          "verify": 0 /* enabled stages only */}}
}
```

**Counts invariant (test-asserted):**
`emitted + dropped_dup + dropped_lowq + dropped_verify + failed + bad_input = scanned + generated
[+ fanout]` (the `fanout` term is v1.7: present only under `classify.assignment = "multi"`).
generate_only degenerates to `emitted + dropped_* + failed = generated [+ fanout]`
(scanned = bad_input = 0).
v1.8 — with segment enabled the FULLY EXPANDED form (spec §6.4) is:

`emitted + dropped_dup + dropped_lowq + dropped_verify + dropped_noise + failed + bad_input
+ absorbed = scanned + generated + fanout + episodes`

(new on the left: `dropped_noise`, `absorbed`; new on the right: `episodes`; disabled
features contribute 0 and the form degrades to the previous line byte-identically).
Breaker-trip runs (v1.6 partial delivery) extend it with `+ unprocessed` on the left side;
`counts.unprocessed` is computed by M10 at finalize as the balancing residual — records scanned
or generated that reached no terminal count (emitted/dropped_*/failed/bad_input) when the run
tripped, which includes generated-but-never-batched records in generate_only — it is NOT a
MetricsSink counter and appears only on tripped runs. v1.8 (S18): in STREAM MODE the
`unprocessed` key appears on "breaker trip **OR** `interrupted = true`" (SIGINT over the
session buffer strands in-flight records); the residual computation carries the expanded
sides (`+ episodes` on the source side, `+ absorbed + dropped_noise` among the terminal
counts). Non-stream interrupted runs keep a PROVABLY ZERO residual and never emit the key
(regression anchor).
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
`generate.sample_validator` is set, v1.5). v1.7 additions: `counts.fanout` (owner M10, R9);
`classify.classes.<name>` / `classify.fallback` / `classify.failures` /
`classify.multi_label_records` (owner M13, §7.13; `classify.fallback` surfaces as the report
key `classify.fallback_count`); tie-rate inputs
`quality.tie_outcomes.<crit>` / `quality.tie_comparisons.<crit>` (v1.5 report drivers) become
pool-dimensioned `quality.tie_outcomes.<pool>.<crit>` / `quality.tie_comparisons.<pool>.<crit>`
when classify is enabled (R12; classify disabled keeps the flat `<crit>` key form unchanged).
v1.8 additions: `counts.episodes` / `counts.absorbed` / `counts.dropped_noise` (owner M10,
§7.9); `segment.failures` and the report-only M14 fields `segment.below_min_len` /
`segment.digest_poor_frames` (surfacing as `report.stream.below_min_len` /
`.digest_poor_frames` / `.segment_failures` — counter key names **[FROZEN HERE]**);
`extract.transitions` / `extract.fallback_steps` / `extract.failures` /
`extract.by_type.<action_type>` (owner M15, §7.15); `verify.membership_repairs` /
`verify.boundary_flags` / `verify.defects.<kind>` (owner M7, §7.6 — surfacing as the
`report.stream.verify` sub-block); `report.stream.sessions` maps from `IngestReport.sessions`
(owner M2, §7.1 — not a MetricsSink counter), `report.stream.episodes`/`mean_episode_len`/
`absorbed`/`dropped_noise` derive from the M10 tallies.

Counter OWNERSHIP (normative): `counts.*` keys are incremented ONLY by M10 (orchestrator),
derived from batch tallies / EmitResult — stages must never touch them (double-count).
v1.7: this includes `counts.fanout` — M10 meters it as the len-delta around the classify
stage (§7.9); M13 never increments any `counts.*` key.
v1.8: likewise `counts.episodes` (len-delta around the segment stage) and
`counts.absorbed`/`counts.dropped_noise` (post-emit tallies) belong to M10 — M14 never
increments any `counts.*` key.
Stage-scoped keys are incremented only by their stage: `dedup.*` by M3, `quality.judgment_failures`
by M4, `annotate.sc_disagreements` by M5, `generate.buckets.*` by M6 (`survived_dedup` = records
surviving M6's own MinHash novelty filter against seeds + siblings; M3 still dedups generated
records on re-flow), `classify.*` by M13 (v1.7), `quality.tie_*` by M4, `segment.*` by M14,
`extract.*` by M15, `verify.membership_repairs`/`verify.boundary_flags`/`verify.defects.<kind>`
by M7 (v1.8).

### 9.4 Atomic delivery

Main output (and sidecar) is appended to `<name>.part` with per-batch flush; finalize = fsync +
`os.rename` to the target name. At any instant the directory holds either the `.part` or the
final file, never a half-written final file — every delivered line is complete and valid.
v1.6: a circuit-break finalize ALSO renames (partial delivery of completed batches, spec 3.10.3
熔断交付), so the final name appearing no longer implies the whole input was processed —
consumers judge run completeness by `report.run`: `interrupted=false` AND `circuit_broken=false`
(the exit code alone is insufficient — a graceful-SIGINT run delivers and exits 0), with
`counts.unprocessed` quantifying the breaker-trip gap. Unwritable output
(exit 4 at open) and unhandled crashes leave `.part`; graceful SIGINT finalize renames.

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

v1.8 sequence variant (`record.kind == "sequence"`, S5/S6 — segment ORDER and the step-line
format are frozen verbatim; system message unchanged):

```
user (current record, sequence form — one user message, parts in this exact order):
  ① text part:  [动作序列]                    ← section omitted ENTIRELY when
                                                item.transitions is None
                {index}. {action_type}（对象: {target|—}；值: {value|—}）{description}
                                              ← one line per Transition, index ascending;
                                                null target/value render as the char "—"
  ② per kept keyframe (keyframe ordinal i of k, member ordinal m; selection per §7.4):
     text part:  [关键帧 {i}/{k}·成员 {m}]
     image part: member.image                 (encoded by M9 at call time)
  ③ text part:  [成员帧摘要]                  ← ALWAYS-PRESENT closing section
                {frame_digest of EVERY member, one per line, member order, total bounded}
```

**Template invariant (S6): the final part is ALWAYS the ③ text section** — the M7 repair
suffix (§10.5) concatenates onto `parts[-1].text`; an image-final message would silently
render "None\n…" and drop the last frame. The `[动作序列]` line format
`{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}` is **[FROZEN HERE]**.

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

v1.8 sequence records (spec 3.4.3 sequence row — applies to the record-content section of
BOTH this template and §10.3): a `kind == "sequence"` record renders as TEXT ONLY (no image
parts even in UI modality — the §6.3 rule-34 quality relaxation), two subsections in order
**[FROZEN HERE]**:

```
[步骤序列]                                    ← omitted entirely when transitions is None
{index}. {action_type}（对象: {target|—}；值: {value|—}）{description}（摘取兜底）
                                              ← same line format as §10.1; the trailing
                                                「（摘取兜底）」 suffix appears ONLY on
                                                fallback steps (Transition.detail.kind ==
                                                "extraction_invalid", S16) so fallback
                                                steps stay distinguishable from
                                                LLM-confirmed "other"
[成员帧摘要]
{frame_digest of every member, one per line, member order, total bounded}
```

In pairwise judging the two subsections sit inside the `[记录 X]` content slot (labels
unchanged); in §10.3 pointwise they replace `{record content}`. The `excerpt` trace tier for
sequences carries the first 200 chars of the member-digest rendering (§7.3).

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

v1.8 stream variant (sequence envelopes only, spec 3.7 stream branch — structure per SPEC
§3.5: five-kind defect explanation in system, the six-section user order; wording
**[FROZEN HERE]**; validated against `defect_verdict_schema()` §10.7, NOT `VERDICT_SCHEMA`):

```
system:
  你是标注质量审核员。给定任务指令、动作序列、边界余量与首末帧截图，独立判断该序列
  （episode）的标注是否合格。
  评审维度: ① 是否遵循任务指令 ② 与动作序列及首末帧证据的事实一致性 ③ 字段语义是否正确填写
  ④ 段边界与成员构成是否成立（对照下列缺陷类型）
  {verify.extra_criteria}                   ← line omitted when empty
  缺陷类型（发现即列入 defects，可为空数组）:
  - label_mismatch: 标注的任务标签与序列证据不符
  - off_task_members: 段内混入与任务无关的成员帧（members 列出这些成员帧 id）
  - missing_head: 段首缺少任务起点帧（结合边界余量判断）
  - missing_tail: 段尾缺少任务终点帧（结合边界余量判断）
  - missing_members: 段中缺失成员帧（members 列出可指认的帧 id，无从指认则为 null）
  先逐维度给出简短意见，再列缺陷表，最后给结论。
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"critiques": [{"aspect": <维度>, "opinion": <一句话意见>}, ...],
   "defects": [{"kind": <缺陷类型>, "members": <帧 id 数组|null>,
                "position": <位置说明|null>, "detail": <一句话>}, ...],
   "verdict": "pass"|"fail"}
user (one message, six sections IN THIS ORDER):
  text part:  [任务指令] {annotate.instruction — class-effective value under classify}
  text part:  [动作序列] {item.transitions rendered per the §10.1 line format;
                          section omitted when transitions is None}
  text part:  [边界余量] {frame_digest of the k=2 frames beyond EACH segment boundary,
                          each annotated with its fate: noise / 相邻段序数 / 无}
  text part:  [首帧截图]
  image part: first member's image
  text part:  [末帧截图]
  image part: last member's image
  text part:  [标注结果] {json.dumps(annotation.output, ensure_ascii=False)}
```

A `fail` verdict with an empty defects array is normalized code-side to one default
`label_mismatch` entry (S7, §7.6). The non-stream template above is byte-unchanged
(regression anchor).

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

v1.7 adds a fifth internal schema (M13; verbatim from spec 3.13.3):

```python
def classification_schema(class_names: list[str], assignment: str,
                          max_labels: int, with_reason: bool) -> dict:
    if assignment == "single":
        props: dict = {"class": {"type": "string", "enum": list(class_names)}}
        required = ["class"]
    else:
        props = {"classes": {"type": "array",
                             "items": {"type": "string", "enum": list(class_names)},
                             "minItems": 1, "maxItems": max_labels}}
        required = ["classes"]
    if with_reason:
        props["reason"] = {"type": "string"}
        required += ["reason"]
    return {"type": "object", "properties": props,
            "required": required, "additionalProperties": False}
```

NOTE (R1, normative): the multi form deliberately carries **NO `uniqueItems`** — OpenAI strict
structured output and some constrained-decoding gateways hard-reject it, and L0 passes the
schema through unconditionally. Duplicate labels are removed by M13's code-side normalization
AFTER M8 validation (a narrowing of an already-validated set, §7.13); the internal-schema
keyword set stays at zero growth.

v1.8 adds three internal schemas (M14/M15/M7-stream; the first two verbatim from spec
3.14.3 / 3.15.3, the third per the v1.8 dev spec §3.5/S7):

```python
def segment_window_schema(frame_count: int, with_reason: bool) -> dict:
    relations = ["continues", "advances", "returns_to_entry", "context_switch", "interruption"]
    item_props = {"index": {"type": "integer", "minimum": 0, "maximum": frame_count - 1},
                  "relation": {"type": "string", "enum": relations}}
    required = ["index", "relation"]
    if with_reason:
        item_props["reason"] = {"type": "string"}
        required = ["index", "relation", "reason"]
    return {"type": "object",
            "properties": {"frames": {"type": "array",
                "items": {"type": "object", "properties": item_props,
                          "required": required, "additionalProperties": False},
                "minItems": frame_count, "maxItems": frame_count}},
            "required": ["frames"], "additionalProperties": False}


def action_schema() -> dict:
    actions = ["click", "long_press", "input_text", "scroll", "drag", "open_app",
               "app_switch", "navigate_back", "navigate_home", "wait", "other"]   # 11 值（S15）
    return {"type": "object",
            "properties": {"action_type": {"type": "string", "enum": actions},
                           "target": {"type": ["string", "null"]},
                           "value": {"type": ["string", "null"]},
                           "description": {"type": "string"}},
            "required": ["action_type", "target", "value", "description"],
            "additionalProperties": False}


def defect_verdict_schema() -> dict:
    kinds = ["label_mismatch", "off_task_members", "missing_head", "missing_tail",
             "missing_members"]
    return {"type": "object",
            "properties": {
                "critiques": {"type": "array",
                    "items": {"type": "object",
                              "properties": {"aspect": {"type": "string"},
                                             "opinion": {"type": "string"}},
                              "required": ["aspect", "opinion"],
                              "additionalProperties": False}},
                "defects": {"type": "array",
                    "items": {"type": "object",
                              "properties": {"kind": {"type": "string", "enum": kinds},
                                             "members": {"type": ["array", "null"],
                                                         "items": {"type": "string"}},
                                             "position": {"type": ["string", "null"]},
                                             "detail": {"type": "string"}},
                              "required": ["kind", "members", "position", "detail"],
                              "additionalProperties": False}},
                "verdict": {"type": "string", "enum": ["pass", "fail"]}},
            "required": ["critiques", "defects", "verdict"],
            "additionalProperties": False}
```

Notes binding on the three (S7 / R1 family): ALL top-level keys and ALL defect sub-keys are
`required` — optionality is expressed ONLY via the nullable unions `["array","null"]` /
`["string","null"]` (OpenAI strict mode hard-rejects optional properties; L0 passes schemas
through unconditionally); **no `uniqueItems` anywhere** (index/label de-duplication is
code-side post-validation — first-wins in §7.14, set-narrowing in §7.13); `minItems ==
maxItems == frame_count` pins the window array length (judgment_schema construction). All
three are INTERNAL schemas: never counted in `schema_engine.resolved_at`, never passed
through the L2.5 `output.validator` hook. `defect_verdict_schema`'s critiques shape is
byte-identical to `VERDICT_SCHEMA`'s (the feed-back/merge chain consumes them unchanged);
critiques/defects precede verdict — reason-then-conclusion, same rationale as
`VERDICT_SCHEMA`. The non-stream verify path keeps `VERDICT_SCHEMA`; the two verdict
schemas co-exist (S7).

### 10.8 M13 classification prompt (spec 3.13.3, verbatim)

```
system:
  single: 你是数据分类员。阅读待分类数据，判断它属于以下类别中的哪一类。类别表：
  multi:  你是数据分类员。阅读待分类数据，判断它适用于以下哪些类别（至少 1 类，至多 {max_labels} 类）。类别表：
  - {name}: {description}                       ← 按 [[classify.classes]] 声明序逐类一行
  {classify.instruction}                        ← 可选补充说明；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  single: {"class": <类名>[, "reason": <一句话理由>]}
  multi:  {"classes": [<类名>, ...][, "reason": <一句话理由>]}   ← reason 仅请求时出现于两式
user (对每条配置了 examples 的类，按声明序；类内按数组序):
  [类别示例·{name}] {example}
user (当前记录):
  文本模态: [待分类数据] {record.text}
  UI 模态:  [屏幕截图] <image: base64>
           [UI 控件树] {record.ui_tree.serialize(max_chars=input.ui_tree_max_chars)}
```

`single:` / `multi:` prefixes select the `classify.assignment` variant of that line — exactly
one is emitted. `reason` is requested iff `trace.enabled` and `"classify"` in `trace.channels`
(R29, §7.13); when not requested, the structure line carries no reason fragment in either
variant. UI modality: the current-record user message is THREE parts — text `[屏幕截图]`, the
image part (encoded by M9 at call time), text `[UI 控件树]\n{serialize(...)}` — the same
single-record assembly shape as §10.1 (R27). Deterministic string concatenation throughout;
class table and per-class examples follow `[[classify.classes]]` declaration order.

v1.8 sequence variant (`record.kind == "sequence"`, spec 3.13.3 sequence row — system and
few-shot messages unchanged; the current-record user message becomes):

```
user (current record, sequence form):
  text part:  [待分类数据·序列]
              {frame_digest of the members, one per line, member order — TOTAL capped at
               input.ui_tree_max_chars: first/last members always kept, middle entries
               truncated WHOLE, capped output ends with the marker line
               "…(truncated N members)"}
  (UI modality only — classify stays in the §6.3 rule-34 vision set:)
  text part:  [首帧截图]
  image part: first member's image             (encoded by M9 at call time)
```

Section label `[待分类数据·序列]`, the `[首帧截图]` label and the truncation-marker line
are **[FROZEN HERE]**. Text-modality sequences carry the digest part only.

### 10.9 M14 segment window-verdict prompt (spec 3.14.4, verbatim)

```
system:
  你是屏幕操作流的分段审核员。下面给出同一会话中按时间顺序排列的 {N} 帧状态摘要
  （含相邻帧的确定性变更提示）。按三步作业：
  一、双向上下文概括：通读全窗，把握每帧之前若干帧正在进行的活动与之后若干帧的走向，再判断该帧。
  二、逐帧关系分类：对每一帧，判断它相对进行中活动的功能角色，只能从以下封闭词表中取恰一值：
  - continues: 同一流程的推进。
  - advances: 屏幕或 App 变了，但可见的任务实体延续（验证码、订单号、餐厅名等跨屏出现）——
    跨 App 的同一任务属此值，不是边界。
  - returns_to_entry: 回到入口/搜索/桌面后开启新流程（同 App 背靠背任务的断点）。
  - context_switch: 交互对象与环境不连续且无实体延续——相关但无实体延续的新流程也取此值。
  - interruption: 与前后活动均无关的短暂插入（通知、弹窗、误触）。
  三、只输出逐帧关系，不判断边界（边界由既定规则从关系推导）。
  锚定约定：分段粒度取「完整任务」层级（整段录屏之下一层）；只看前台 App/前台窗口，
  忽略状态栏、后台通知等背景变化。
  {segment.context}                              ← 可选域上下文；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"frames": [{"index": <窗内帧序号>, "relation": <词表值>[, "reason": <一句话理由>]}, ...]}（恰 {N} 项）
user（窗内逐帧，一帧一段）:
  [帧 {i}] {frame_digest(frame_i, segment.digest_max_chars)}
  [帧 {i} 变更] {tree_diff(frame_{i-1}, frame_i) 的文字化摘要}      ← i ≥ 1；窗首帧无此行
  （segment.use_vision = true 时：每帧摘要 text Part 前附该帧 kind="image" 的 Part，3.9.2）
```

Both anchors are hard-coded in the template text and never vary with configuration:
granularity = the "complete task" level (GEBD "1 level deeper"), attention = foreground
App/window only (GEBD dominant subject). The five-value relation vocabulary is fixed and
domain-independent; the `advances`/`context_switch` divide is pinned to VISIBLE-ENTITY
CONTINUITY — a related new flow without entity continuity is `context_switch` (a boundary,
S32). The LLM never answers the boundary question directly; boundary/noise are code-side
lookups (deductive mapping table, §7.14; the `reason` fragment appears in the structure line
only under `with_reason`, §8.1 †). Response validated against
`segment_window_schema(N, with_reason)` (§10.7).

### 10.10 M15 extract prompt (spec 3.15.4, verbatim)

```
system:
  你是屏幕操作流的动作摘取员。给定同一操作流中相邻的前后两帧屏幕状态，推断用户在两帧之间
  执行的动作。action_type 只能取以下值：
  - click / long_press / drag: 点击 / 长按 / 拖拽某控件
  - input_text: 在输入框键入文本
  - scroll: 滚动屏幕或列表
  - open_app: 打开一个应用；app_switch: 切换到另一已打开的应用
  - navigate_back / navigate_home: 系统返回 / 回到桌面
  - wait: 无用户交互，仅等待界面加载或变化
  - other: 无法归入以上任何一类（把语义写进 description）
  锚定约定：前一帧是动作发生前最后一个稳定状态，后一帧是动作完成后的首个稳定状态；推断
  二者之间发生的单个语义动作；若变化由多个低层事件构成（连续滚动、连续键入），归并为一个
  语义动作。
  {instruction}                                 ← 可选补充说明（per-label 有效值）；缺省省略此行
  输出必须是符合以下结构的单个 JSON 对象，不输出任何其他内容：
  {"action_type": <词表值>, "target": <目标控件文本引用或 null>,
   "value": <动作参数或 null>, "description": <一句话动作描述>}
user（单条消息多 Part——「text 标签 + image」组装惯例同 3.5.2/3.13.3；一请求 2 图）:
  text part:  [前一帧截图]
  image part: s_i.image                          （M9 调用时编码，3.9.2）
  text part:  [后一帧截图]
  image part: s_{i+1}.image
  text part:  [树变更摘要] {tree_diff(s_i.ui_tree, s_{i+1}.ui_tree) 的文字化}
                                                 ← include_diff = true 时；false 整段省略
              [前后帧树摘要] {frame_digest(s_i)} → {frame_digest(s_{i+1})}
```

Field semantics (verbatim-frozen table; vocabulary legality is enforced by the schema enum,
field semantics are anchored by the template wording):

| action_type | `target` semantics | `value` semantics |
|---|---|---|
| `click` / `long_press` / `drag` | the target widget's **text reference**, precedence text → content_desc → 类名+序号; null when unidentifiable | null |
| `input_text` | text reference of the input box being typed into (same precedence) | the typed text — **aggregation semantics**: a "focus click + typing" within one adjacent pair merges into ONE input_text step; the focus click is never a separate step |
| `scroll` | scroll-container reference; null when unidentifiable | direction, limited to `up` / `down` / `left` / `right` (template-anchored four values; code-side lowercase normalization) |
| `open_app` / `app_switch` | null | the application name |
| `navigate_back` / `navigate_home` / `wait` | null | null |
| `other` | best-effort object reference or null | null (all semantics go into description) |

Two binding design notes (spec 3.15.4): ① `target` uses TEXT REFERENCES, never coordinates —
extract is post-hoc annotation, not an executor; text references and center coordinates are
equivalent to an LLM, and `max_image_px` downscaling would break the coordinate
correspondence with the original screenshot; ② the `[树变更摘要]` section injects the
STRUCTURAL tree diff (never a pixel diff — pixel-diff injection is a reported negative
result): deterministic evidence that shortens the visual inference distance
(`extract.include_diff`, default on, ablatable — S14). The final part is the always-present
text section (S6 invariant holds here too). Response validated against `action_schema()`
(§10.7); the closing `[前后帧树摘要]` line is ALWAYS present.

---

## 11. Cross-cutting conventions (binding)

1. **Async everywhere LLM is involved.** `Stage.run`, `complete_validated`, `complete`, `embed`,
   `probe`, `Orchestrator.run` are `async def`. Record-level concurrency inside a stage via
   `asyncio.gather`; stages are serial within a batch (barrier); batches are serial.
2. **Stages never remove items** — status flips only; `generate` returns a new list instead
   (v1.7 ②a: classify multi may tail-append; v1.8 ②b: segment may tail-append sequence
   envelopes and absorb members, with the M7 bidirectional repair exemption — §5).
3. **Single-record failures never escape**: `item.errors.append(StageError(...))` +
   `status="failed"` + `error` trace event; the run continues. Record-level isolation is absolute.
4. **Determinism.** All sampling RNGs derive from `run.seed` exactly as §5; temperature default
   0.0; generate pre-draws its (llm, style, seeds) plan in call-index order before dispatch;
   top_ratio ties broken by record id ascending; same input + same seed ⇒ byte-identical pairing
   plan and selection decisions. Retry jitter and key-pool selection are exempt (timing only;
   key selection is deterministic least-in-flight and never changes what data is produced, v1.6).
5. **No data persistence**: no temp files beyond the declared output channels (`.part` files are
   part of output delivery); no caches, checkpoints, or cross-run state; the closed list of
   cross-batch survivors is: DedupIndex, MetricsSink counters, M9 usage — all content-free —
   plus, v1.8 stream mode only, M2's unclosed-session buffer (≤ `session_max_len` Record
   metadata entries, images still lazy) and M10's single pending overflow session (next-fit's
   open bin, §7.9) — both process-memory only, released as soon as they are packed/consumed;
   neither is a new disk surface (spec §2.6).
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
    written and — v1.6 revision (stakeholder decision, spec 1.6 ②) — `.part` IS renamed:
    completed batches are delivered with `run.partial_delivery=true` + `counts.unprocessed`
    (pre-v1.6 rule was ".part NOT renamed").
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
    without `--show` lists names; exception→exit-code mapping lives only in
    `labelkit/cli/main.py`.
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
25. v1.6 key pool (spec 3.9.3, decisions spec 1.6 2026-07-03): `api_key_envs`/`api_keys`
    are normalized tuples (scalar form → 1-tuple; `api_key_env`/`api_key` mirror element 0);
    per-attempt least-in-flight key selection, declaration-order tie-break (deterministic,
    seed-exempt); per-key 429 cooldown (Retry-After in full, else jittered exponential capped
    at 300 s); auth failure disables the key and is absorbed silently by rotation (no retry
    consumed, nothing fed to the breaker) unless it is the LAST live key — then hard-trip,
    preserving v1.5 P2-3 semantics for pools of 1; quota-as-403 treated as auth (no body
    sniffing); parking bounded by `run.max_park_s`, overrun → retry-exhaustion path (P1-1
    preserved); `probe_all()` additive beside the frozen `probe()`; `ProbeResult.key_env`,
    `KeyUsage`, `ProfileUsage.keys/parked_calls/parked_ms`, exception `key_env` fields all
    additive; per-key observability (events, report) carries env-var NAMES only, never values.
26. v1.6 breaker-trip delivery (spec 3.10.3/3.11.2, decision spec 1.6 ②): `Emitter.finalize`
    delivers on circuit break (`deliver=True`); `deliver=False` remains dry-run-only;
    `run.partial_delivery` present only when true; `counts.unprocessed` = balancing residual
    computed by M10 at finalize, only on tripped runs; the consumer signal for "run processed
    all input" moves from "final filename exists" to "report.run.interrupted=false AND
    circuit_broken=false" (exit code alone is insufficient: graceful SIGINT delivers and
    exits 0).
27. v1.7 classify (feature spec `docs/dev/SPEC-classify-operator.md`, rulings R1–R30;
    2026-07-07). Key frozen points: `build_annotate_prompt` / `annotate_record` gain a
    TRAILING optional `label: str | None = None` — an additive revision of the §7.4 frozen
    signatures whose `None` default reproduces pre-v1.7 behavior with zero changes at old
    call sites (R2); `counts.fanout` is OWNED BY M10, metered as the `len(batch)` delta
    around the classify stage — M13 never touches `counts.*` (R9); `on_error="fallback"`
    writes NO entry into `item.errors` — evidence goes to `Classification.detail` + the
    `error` trace event + `classify.fallback`, keeping the §9.2 rejects attribution
    (`errors[0]`) unpolluted (R4); `classification_schema` carries NO `uniqueItems`
    (L0 strict-mode pass-through compatibility) — duplicate-label dedupe is code-side
    normalization after M8 validation (R1). Additive-only surface elsewhere: rejects refs
    lines grow to six keys (`label`), bucket keys gain the `<class>×` prefix, and events
    gain `pool`/`label`/`fanout` payload fields ONLY when classify is enabled — classify
    disabled is byte-identical to v1.6 output except `_meta.classification: null`. The new
    module section is numbered §7.13 AFTER the pre-existing §7.12 CLI section so frozen
    §7.x anchors in code and docs stay valid.
28. v1.8 stream segmentation & action extraction (feature spec
    `docs/dev/SPEC-stream-segmentation.md`, rulings S1–S32; 2026-07-13). Key frozen points,
    in ruling order:
    - contract ②b (S3): segment absorbs members / tail-appends sequence envelopes; the M7
      repair path may rewrite member status BIDIRECTIONALLY between `absorbed` and
      `dropped_noise` — the contract's only reverse exemption; flipping back to `active` is
      forbidden; each member is absorbed by at most one sequence envelope (§5);
    - trace channels grow 8 → 10 (`"segment"`, `"extract"`; channel = stage name, S1);
      event names stay `segment.session`/`segment.boundary`/`extract.step`; the
      `ingest.disorder` event (S19/S20 monotonicity rejects) joins the catalog with
      constant `EV_INGEST_DISORDER` (§7.11/§8.1);
    - `judge_window` / `extract_transition` are PUBLIC direct-call surfaces for M7's stream
      repair driver — the second and third sanctioned operator-to-operator imports after
      the verify→annotate hook (§7.14/§7.15, ground rules);
    - PER-LABEL extraction under multi fan-out (S9): every sibling envelope extracts
      independently under its own label's effective `[class.<label>.extract]` instruction
      (×k cost accepted — the whitelist promise is honored; `transitions` is per-envelope;
      clones start with `transitions = None`); dry-run reports the ×1 lower bound;
    - two-phase batch-level member surgery (S8): concurrent review → SYNCHRONOUS surgery in
      batch position order → concurrent seam re-extraction → synchronous rebuild →
      concurrent re-annotation; multi siblings get mark-only membership handling; no
      re-scoring after repair (`_meta.stream.repaired`);
    - whole-session NEXT-FIT batching (S21; one open bin; oversized sessions hard-split
      with the `session_split` duck-typed mark); `Session.session_id =
      sha256("\n".join(record ids))[:16]` and the `Session` dataclass shape are frozen in
      §7.1 [FROZEN HERE];
    - sequence records inherit `ref` from their FIRST member (S24; line_no/pair_index
      convention preserved; full provenance in `_meta.stream.member_sources`); sequence id
      = sha256 over member ids, fixed at formation (§3);
    - redaction: `_DATA_KEYS = {"target","value"}` stripped at none/refs;
      `_FREE_TEXT_KEYS += "description"` (S27, §8.3); the three segment/extract events are
      trace-only (no stderr mirror);
    - trajectory rubric (S29): empty `quality.rubric` under `segment.enabled` resolves to
      `"default:trajectory"` (packaged `default_trajectory.toml`, rubric name
      `default-trajectory-v1`); trajectory + `extract.enabled=false` → warning
      ("步骤" degrades to "帧间变化");
    - `annotate.sequence_frames` ∈ [2, 100] with the `> 20 ∧ max_image_px > 2000` WARN
      linkage (S28: Anthropic 400 hard-reject, not a resize) and the zero-rng downsample
      formula `idx_i = ⌊i·(n−1)/(k−1)⌋` (§6.1/§6.3);
    - action vocabulary fixed at ELEVEN values (S15: AndroidControl full set ∪
      UI-TARS-mobile increment + `other`); `extract.include_diff` toggle defaults ON
      (structural tree diff, never pixel diff — S14); `extract.on_error =
      "fallback"|"fail"` (S16 — never "unknown"); fallback steps carry `Transition.detail`
      evidence and render with the 「（摘取兜底）」 suffix in quality prompts (§10.2);
    - `segment.min_len` applies ONLY to LLM-refined segments (S11); its casualties get
      reason `"below_min_len"` ≠ `"noise"` and an independent counter;
    - timestamp parsing thresholds (S20): numeric `v < 0 ∨ v ≥ 1e14` = failure, `v < 1e11`
      = seconds, `[1e11, 1e14)` = milliseconds; ISO strings via `fromisoformat`; naive =
      UTC; failures walk `stream.on_disorder`;
    - `counts.unprocessed` appearance widens to "breaker ∨ interrupted" in STREAM MODE ONLY
      (S18); the conservation identity gains `absorbed`/`dropped_noise`/`episodes` terms
      (§9.3); non-stream interrupted runs keep zero residual (regression anchor);
    - sequence dedup separator `"\x1e"` (ASCII RS, S10, §7.2); sequence quality scoring is
      pure text — the single rule-4 vision relaxation (S30, §6.3 rule 34);
    - prompt wording frozen here where the spec fixed only structure: the §10.1 sequence
      segment order and step-line format (S6 — text-final invariant), the §10.2/§10.3
      sequence record sections, the §10.5 stream verify system/user wording (five-kind
      defect explanation + six-section order), the §10.8 `[待分类数据·序列]`/`[首帧截图]`
      labels and member-truncation marker;
    - the new module sections are numbered §7.14/§7.15 AFTER the pre-existing §7.13 (same
      anchor-stability rationale as v1.7). Segment/extract disabled is byte-identical to
      v1.7 output except `_meta.stream: null`.

— End of contract. —

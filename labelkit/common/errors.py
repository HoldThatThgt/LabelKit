"""Exception hierarchy (spec §4.3) and error classification codes (spec §7.6)."""
from __future__ import annotations

import enum
from typing import Literal


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
    """M9: retryable provider error with retries exhausted (v1.6: incl. park-budget
    overrun, run.max_park_s). Record-level → status='failed'."""
    def __init__(self, message: str, profile: str, retries: int,
                 key_env: str | None = None):
        self.profile = profile
        self.retries = retries
        self.key_env = key_env                # v1.6: env-var NAME of the last key tried (pools)
        super().__init__(message)


class ProviderFatalError(LabelKitError):
    """M9: non-retryable provider error (401/403/400/404, dims mismatch). Feeds the circuit
    breaker; a streak >= run.fatal_error_threshold ends the run with exit code 4.
    v1.6 pools: an auth failure absorbed by key rotation raises nothing — for auth this is
    raised only when the LAST live key gets disabled (spec 3.9.3)."""
    def __init__(self, message: str, profile: str, status_code: int | None = None,
                 key_env: str | None = None):
        self.profile = profile
        self.status_code = status_code
        self.key_env = key_env                # v1.6: env-var NAME of the failing key (pools)
        super().__init__(message)


class ContextOverflowError(LabelKitError):
    """v1.11 (V16/V24): the unified context-overflow signal. Record-level →
    status='failed', kind='context_overflow' (§7.6) → rejects; run continues.
    phase='precheck' — the M9 pre-dispatch invariant check fired (V16, zero provider
    interaction), or a packing layer found even the minimal semantic unit unfittable
    (V10 — recorded directly by the operator, no exception crossing);
    phase='reactive' — a real provider interaction identified overflow: budget-gated
    400 body-sniff hit, or the 200-shaped `model_context_window_exceeded` termination
    (V20/V24). M9 itself NEVER feeds `record_provider_result(fatal=True)` for this
    exception and burns no regular retry — the reactive-400 terminal is fed exactly
    once by the OWNING operator after its bounded degrade-retries exhaust (A7; §7.8
    breaker matrix)."""
    def __init__(self, message: str, phase: Literal["precheck", "reactive"],
                 profile: str | None = None):
        self.phase = phase
        self.profile = profile                # additive carrier (trailing kwarg)
        super().__init__(message)


class OutputTruncatedError(LabelKitError):
    """v1.11 (V11): the response terminated by hitting the output cap —
    finish_reason='length' (openai) / stop_reason='max_tokens' (anthropic): input fit
    the window, the model wrote max_output_tokens full. Record-level →
    status='failed', kind='output_truncated' → rejects (own bucket); the truncated
    text NEVER enters the L1–L3 repair loop, and the breaker is never fed (the HTTP
    interaction succeeded — `llm.call` stays status='ok')."""
    def __init__(self, message: str, profile: str | None = None,
                 finish: str | None = None):
        self.profile = profile                # additive carriers (trailing kwargs)
        self.finish = finish
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
    CLASSIFICATION_INVALID = "classification_invalid"        # v1.7: M13, M8 repair exhausted —
                                                             # fallback keeps record; "fail" → rejects
    SEGMENTATION_INVALID = "segmentation_invalid"            # v1.8: M14, M8 repair exhausted —
                                                             # "keep" = whole session survives as one
                                                             # episode (trace in _meta.stream.degraded);
                                                             # "fail" = session members failed → rejects
    EXTRACTION_INVALID = "extraction_invalid"                # v1.8: M15, M8 repair exhausted —
                                                             # "fallback" = step records
                                                             # action_type="other" (trace in
                                                             # Transition.detail, not item.errors);
                                                             # "fail" = episode failed → rejects
    STITCH_INVALID = "stitch_invalid"                        # v1.9: M16, M8 repair exhausted —
                                                             # "keep" = episode candidate opens its
                                                             # own thread (evidence via error event
                                                             # + stitch.failures, never item.errors);
                                                             # "fail" = episode-candidate envelope
                                                             # failed → rejects (member frames stay
                                                             # absorbed; rescue candidates never
                                                             # take the fail path, B-2)
    JUDGMENT_INVALID = "judgment_invalid"                    # M4, comparison-level → counts as tie
    SCHEMA_VIOLATION = "schema_violation"                    # M8 L3 exhausted → failed → rejects
    CALLBACK_VIOLATION = "callback_violation"                # v1.5: L3 exhausted, remaining
                                                             # violations all from output.validator
    PROVIDER_RETRYABLE_EXHAUSTED = "provider_retryable_exhausted"  # M9 → failed, feeds breaker window
    PROVIDER_FATAL = "provider_fatal"                        # M9 run-level, feeds breaker directly
    CONTEXT_OVERFLOW = "context_overflow"                    # v1.11: ContextOverflowError — precheck
                                                             # (V16 throat / V10 minimal unit) or
                                                             # reactive (V20/V24) → failed → rejects;
                                                             # counted in report.budget.
                                                             # overflow_records; breaker matrix §7.8
    OUTPUT_TRUNCATED = "output_truncated"                    # v1.11: OutputTruncatedError (V11) —
                                                             # output hit max_output_tokens →
                                                             # failed → rejects own bucket; never
                                                             # repaired, never feeds the breaker
    INTERNAL_ERROR = "internal_error"                        # any unexpected exception

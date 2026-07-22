from __future__ import annotations

from labelkit.common.errors import (
    EXIT_CONFIG,
    EXIT_FATAL,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STRICT,
    CircuitBreakerTripped,
    ConfigError,
    ErrorKind,
    InputError,
    InternalError,
    LabelKitError,
    ProviderFatalError,
    ProviderRetryableError,
    SchemaViolation,
)


def test_all_canonical_exceptions_share_labelkit_error_base():
    assert all(
        issubclass(error_type, LabelKitError)
        for error_type in (
            ConfigError,
            InputError,
            ProviderRetryableError,
            ProviderFatalError,
            SchemaViolation,
            InternalError,
            CircuitBreakerTripped,
        )
    )


def test_exit_code_constants_are_exact_and_contiguous():
    assert (EXIT_OK, EXIT_STRICT, EXIT_CONFIG, EXIT_INPUT, EXIT_FATAL) == (0, 1, 2, 3, 4)


def test_config_error_preserves_all_errors_and_newline_string():
    errors = ["first", "second"]
    exc = ConfigError(errors)

    assert exc.errors is errors
    assert str(exc) == "first\nsecond"


def test_provider_errors_preserve_diagnostic_fields_and_message():
    retryable = ProviderRetryableError("timed out", "judge", 5, "KEY_B")
    fatal = ProviderFatalError("unauthorized", "default", 401, "KEY_A")

    assert (retryable.profile, retryable.retries, retryable.key_env) == (
        "judge",
        5,
        "KEY_B",
    )
    assert str(retryable) == "timed out"
    assert (fatal.profile, fatal.status_code, fatal.key_env) == (
        "default",
        401,
        "KEY_A",
    )
    assert str(fatal) == "unauthorized"


def test_schema_violation_preserves_repair_context_and_joins_errors():
    errors = ["/intent: required", "/topic: wrong type"]
    exc = SchemaViolation(errors, '{"intent": 3}', callback_only=True)

    assert exc.errors is errors
    assert exc.raw_last_output == '{"intent": 3}'
    assert exc.callback_only is True
    assert str(exc) == "/intent: required; /topic: wrong type"


def test_plain_errors_keep_standard_exception_string_behavior():
    assert str(InputError("missing input")) == "missing input"
    assert str(InternalError("broken invariant")) == "broken invariant"
    assert str(CircuitBreakerTripped("breaker open")) == "breaker open"


def test_error_kind_values_are_the_frozen_wire_codes():
    assert {member.name: member.value for member in ErrorKind} == {
        "BAD_INPUT_LINE": "bad_input_line",
        "MISSING_PAIR": "missing_pair",
        "INDEX_CONFLICT": "index_conflict",
        "IMAGE_TOO_LARGE": "image_too_large",
        "IMAGE_DECODE_ERROR": "image_decode_error",
        "CLASSIFICATION_INVALID": "classification_invalid",
        "SEGMENTATION_INVALID": "segmentation_invalid",
        "EXTRACTION_INVALID": "extraction_invalid",
        "STITCH_INVALID": "stitch_invalid",
        "JUDGMENT_INVALID": "judgment_invalid",
        "SCHEMA_VIOLATION": "schema_violation",
        "CALLBACK_VIOLATION": "callback_violation",
        "PROVIDER_RETRYABLE_EXHAUSTED": "provider_retryable_exhausted",
        "PROVIDER_FATAL": "provider_fatal",
        "CONTEXT_OVERFLOW": "context_overflow",        # v1.11 (V16/V24)
        "OUTPUT_TRUNCATED": "output_truncated",        # v1.11 (V11)
        "INTERNAL_ERROR": "internal_error",
    }

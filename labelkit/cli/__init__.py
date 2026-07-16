"""LabelKit CLI package and compatibility exports for the former ``cli.py``."""

from labelkit.common.config.model import CliOverrides
from labelkit.common.errors import (
    EXIT_CONFIG,
    EXIT_FATAL,
    EXIT_INPUT,
    EXIT_OK,
    EXIT_STRICT,
    CircuitBreakerTripped,
    ConfigError,
    InputError,
    LabelKitError,
    ProviderFatalError,
)
from labelkit.orchestration.factory import build_stages as _build_stages
from labelkit.orchestration.profile_usage import referenced_profiles

from .commands import _cmd_rubric, _cmd_run, _cmd_validate
from .main import _print_exception, exit_code_for, main
from .parser import (
    _RUBRIC_FILES,
    _overrides_from_args,
    _positive_int,
    build_parser,
)

__all__ = ["build_parser", "exit_code_for", "main", "referenced_profiles"]

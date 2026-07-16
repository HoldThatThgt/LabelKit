"""Compatibility exports for the canonical M5 annotate operator."""
from labelkit.operators.annotate import *  # noqa: F401,F403
from labelkit.operators.annotate import (  # noqa: F401
    _keyframe_indexes,
    _majority_vote,
    _member_digest_lines,
    _voted_keys,
)

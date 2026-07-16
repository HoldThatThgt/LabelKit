"""Compatibility exports for the canonical M3 dedup operator."""
from labelkit.operators.dedup import *  # noqa: F401,F403
from labelkit.operators.dedup import (  # noqa: F401
    _ProbeDetail,
    _build_minhash,
    _dedup_text,
    _l2_normalize,
    _normalize_text,
    _phash_int,
    _shingles,
)

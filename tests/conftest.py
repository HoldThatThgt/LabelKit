"""Shared test config: loads .env for integration credentials, gates integration tests.

Integration tests hit the real z.ai endpoint (no mock LLMs — project policy).
They are marked @pytest.mark.integration and auto-skip when LABELKIT_ZAI_KEY
is not available from the environment or the repo-root .env file.
"""

import os
from pathlib import Path

import pytest

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv() -> None:
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# Real-endpoint parameters shared by all integration tests.
ZAI_BASE_URL = "https://api.z.ai/api/anthropic"
ZAI_MODEL = "glm-5.2"
ZAI_KEY_ENV = "LABELKIT_ZAI_KEY"


def pytest_collection_modifyitems(config, items):
    if os.environ.get(ZAI_KEY_ENV):
        return
    skip = pytest.mark.skip(reason=f"{ZAI_KEY_ENV} not set — integration tests need the real endpoint")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)

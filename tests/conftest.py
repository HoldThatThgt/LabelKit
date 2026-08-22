"""加载真实端点凭据，并按端点 marker 独立门控集成测试。"""

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
DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_MODEL = "deepseek-v4-flash"
DEEPSEEK_KEY_ENV = "LABELKIT_DEEPSEEK_KEY"
LOCAL_LLM_KEY_ENV = "LABELKIT_LOCAL_KEY"


def pytest_collection_modifyitems(config, items):
    """让 DeepSeek 与 z.ai 用例只依赖各自的真实凭据。

    @param config pytest 配置对象；本函数不读取其内容。
    @param items 已收集测试项。
    """
    del config
    for item in items:
        if "integration" not in item.keywords:
            continue
        if "deepseek" in item.keywords and not os.environ.get(DEEPSEEK_KEY_ENV):
            reason = f"{DEEPSEEK_KEY_ENV} not set; DeepSeek integration requires the real endpoint"
            item.add_marker(pytest.mark.skip(reason=reason))
        elif "local_llm" in item.keywords and not os.environ.get(LOCAL_LLM_KEY_ENV):
            reason = f"{LOCAL_LLM_KEY_ENV} not set; local LLM integration requires the real endpoint"
            item.add_marker(pytest.mark.skip(reason=reason))
        elif "zai" in item.keywords and not os.environ.get(ZAI_KEY_ENV):
            reason = f"{ZAI_KEY_ENV} not set; z.ai integration requires the real endpoint"
            item.add_marker(pytest.mark.skip(reason=reason))
        elif not ({"deepseek", "local_llm", "zai"} & set(item.keywords)) and not os.environ.get(ZAI_KEY_ENV):
            reason = f"{ZAI_KEY_ENV} not set; integration requires the real z.ai endpoint"
            item.add_marker(pytest.mark.skip(reason=reason))

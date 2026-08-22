"""测试使用的冻结 LLMClient 运行期对象图装配。"""
from __future__ import annotations

from collections.abc import Mapping

import httpx

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.inference.credentials import RuntimeCredentials
from labelkit.common.inference.llm_client import LLMClient
from labelkit.runtime.resources import ResourceManager


def make_llm_client(
    llm_profiles: Mapping[str, LLMProfile],
    embedding_profiles: Mapping[str, EmbeddingProfile],
    credentials: RuntimeCredentials,
    metrics=None,
) -> LLMClient:
    """从实际 profile 派生资源容量与规范化 HTTP origin。"""
    profiles = [(("llm", name), profile) for name, profile in llm_profiles.items()]
    profiles += [(("embedding", name), profile) for name, profile in embedding_profiles.items()]
    capacities = {key: profile.max_concurrency for key, profile in profiles}
    origins = {key: _origin(profile.base_url) for key, profile in profiles}
    resources = ResourceManager(capacities, origins, metrics)
    return LLMClient(llm_profiles, embedding_profiles, credentials, resources, metrics)


def _origin(base_url: str) -> tuple[str, str, int]:
    """使用 httpx.URL 生成冻结 origin。"""
    url = httpx.URL(base_url)
    port = url.port or (443 if url.scheme == "https" else 80)
    return url.scheme, url.raw_host.decode("ascii"), port

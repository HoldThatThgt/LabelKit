"""M9 — LLM client (spec 3.9, CONTRACTS.md §7.8).

Unified async multi-provider client: message assembly (text / multimodal),
provider adaptation (openai_compatible / anthropic native), structured-output
parameter passthrough, timeout/retry/rate-limit, token & cost metering, and
the ``validate --probe`` connectivity check.

Boundaries (spec 3.9.1): no business parsing (raw text or native structured
payload only — parsing belongs to M8), no response caching, no model routing.

Request bodies and response parsing are pure module-level helpers so they can
be unit-tested without any network.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

import httpx

from labelkit.common.config.model import EmbeddingProfile, LLMProfile
from labelkit.common.contracts.types import ImageRef, Usage
from labelkit.common.errors import (
    CircuitBreakerTripped,
    ProviderFatalError,
    ProviderRetryableError,
)

if TYPE_CHECKING:
    from labelkit.common.observability.obslog import MetricsSink

from labelkit.common.observability.obslog import (
    EV_LLM_CALL,
    EV_LLM_KEY_COOLDOWN,
    EV_LLM_KEY_DISABLED,
    EV_LLM_POOL_PARKED,
)

ANTHROPIC_VERSION = "2023-06-01"          # [FROZEN in CONTRACTS.md §7.8]
STRUCTURED_TOOL_NAME = "emit"             # [FROZEN in CONTRACTS.md §7.8]
_MAX_BACKOFF_S = 60.0                     # backoff cap (spec 3.9.3) — v1.6: non-429 retryables only
_MAX_KEY_COOLDOWN_S = 300.0               # no-Retry-After per-key 429 cooldown cap (spec 3.9.3)
_PARK_SLICE_S = 60.0                      # park sleep slice; breaker re-checked per slice (v1.6)


# ── public dataclasses (CONTRACTS.md §7.8, verbatim shapes) ────────────────

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


@dataclass                                          # v1.6 per-key accumulator (CONTRACTS §7.8)
class KeyUsage:
    calls: int = 0                                 # successful logical calls on this key
    rate_limited: int = 0                          # 429s observed on this key
    disabled: bool = False                         # auth-disabled during this run


@dataclass                                          # mutable per-profile accumulator
class ProfileUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    est_cost_usd: float | None = None              # only when prices configured
    keys: dict[str, KeyUsage] = field(default_factory=dict)
                                                   # v1.6: by env-var name; report emits the
                                                   # sub-object only for pools > 1 (§9.3)
    parked_calls: int = 0                          # v1.6: logical calls that parked ≥ once
    parked_ms: int = 0                             # v1.6: total parked wall-clock


@dataclass(frozen=True)
class ProbeResult:
    profile: str
    ok: bool
    model: str
    latency_ms: int
    error: str | None = None
    key_env: str | None = None                     # v1.6: set by probe_all() on pooled
                                                   # profiles; None on single-key profiles


# ── v1.6 key pool (spec 3.9.3 密钥池行) ────────────────────────────────────

@dataclass
class _KeyState:
    index: int                     # declaration order (tie-break)
    env: str                       # env-var NAME — the only identity ever logged
    key: str = field(repr=False, default="")
    in_flight: int = 0             # HTTP requests currently on the wire
    cooldown_until: float = 0.0    # time.monotonic() deadline (429 cooldown)
    consec_429: int = 0            # cross-call; reset by a success ON THIS KEY
    disabled: bool = False         # 401/403: auth-dead for the rest of the run


class _KeyPool:
    """Per-(kind, profile) in-memory key-pool state. Pure logic — callers
    inject ``now`` so selection/park arithmetic is unit-testable offline."""

    def __init__(self, members: list[tuple[str, str]]):
        self.states = [_KeyState(index=i, env=env, key=key)
                       for i, (env, key) in enumerate(members)]

    @property
    def size(self) -> int:
        return len(self.states)

    def live(self) -> list[_KeyState]:
        return [s for s in self.states if not s.disabled]

    def select(self, now: float) -> _KeyState | None:
        """Least-in-flight eligible key, ties broken by declaration order —
        deterministic, no RNG (timing-only, seed-exempt; spec 3.9.3)."""
        eligible = [s for s in self.states
                    if not s.disabled and s.cooldown_until <= now]
        if not eligible:
            return None
        return min(eligible, key=lambda s: (s.in_flight, s.index))

    def earliest_wake(self, now: float) -> float:
        """Seconds until the earliest live key leaves cooldown (≥ 0). Callers
        guarantee at least one live key."""
        return max(0.0, min(s.cooldown_until for s in self.live()) - now)


def _key_cooldown_upper(base_delay_s: float, consec_429: int) -> float:
    """Upper bound of the no-Retry-After per-key 429 cooldown: full-jitter
    random(0, base × 2^c) with the upper bound capped at 300 s (spec 3.9.3;
    c = the key's cross-call consecutive-429 count)."""
    return min(_MAX_KEY_COOLDOWN_S, base_delay_s * (2.0 ** consec_429))


def _pool_members(prof: "LLMProfile | EmbeddingProfile") -> list[tuple[str, str]]:
    """(env-var name, resolved key) pairs for the profile's key pool (v1.6).
    M1-normalized profiles carry aligned api_key_envs/api_keys; directly
    constructed profiles (tests, probe children) fall back to api_key / the
    environment, mirroring the pre-v1.6 single-key behavior."""
    envs = tuple(prof.api_key_envs) or ((prof.api_key_env,) if prof.api_key_env else ())
    if not envs:
        return [("", prof.api_key or "")]
    keys = tuple(prof.api_keys)
    if len(keys) != len(envs):
        if len(envs) == 1:
            keys = (prof.api_key or os.environ.get(envs[0], ""),)
        else:
            keys = tuple(os.environ.get(e, "") for e in envs)
    return list(zip(envs, keys))


# ── pure helpers: retry math and classification ───────────────────────────

def _is_retryable_status(status: int) -> bool:
    """Retryable = HTTP 408/409/429/5xx (spec 3.9.3); everything else is fatal."""
    return status in (408, 409, 429) or 500 <= status <= 599


def _backoff_delay(retry_no: int, base_delay_s: float, rng: random.Random) -> float:
    """Full-jitter exponential backoff: wait_i = random(0, base * 2^i), upper
    bound capped at 60 s (spec 3.9.3). ``retry_no`` is 1-based (the timeline in
    spec 3.9.4 ③ uses i=2 for the wait after the second attempt)."""
    upper = min(_MAX_BACKOFF_S, base_delay_s * (2.0 ** retry_no))
    return rng.uniform(0.0, upper)


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header: delta-seconds or HTTP-date. None if absent/unparseable."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:  # pre-3.10 parsedate_to_datetime could return None
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ref = now if now is not None else datetime.now(timezone.utc)
    return max(0.0, (dt - ref).total_seconds())


# ── pure helpers: request-body assembly ────────────────────────────────────

def _resolve_temperature(profile: LLMProfile, prompt: PromptBundle) -> float:
    return profile.temperature if prompt.temperature is None else prompt.temperature


def _build_openai_body(profile: LLMProfile, prompt: PromptBundle,
                       response_schema: dict | None) -> dict:
    """POST {base_url}/chat/completions body. Images become image_url data URIs;
    structured output = response_format json_schema strict (spec 3.9.3 / 3.9.4 ①).
    Image bytes are loaded lazily HERE (request-build time) and only live inside
    the returned body."""
    messages: list[dict] = []
    for msg in prompt.messages:
        content: Any
        if len(msg.parts) == 1 and msg.parts[0].kind == "text":
            content = msg.parts[0].text or ""
        else:
            content = []
            for part in msg.parts:
                if part.kind == "text":
                    content.append({"type": "text", "text": part.text or ""})
                else:
                    assert part.image is not None
                    media_type, b64 = part.image.load_base64(profile.max_image_px)
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{media_type};base64,{b64}"}})
        messages.append({"role": msg.role, "content": content})
    body: dict = {
        "model": profile.model,
        "temperature": _resolve_temperature(profile, prompt),
        "max_tokens": profile.max_output_tokens,
        "messages": messages,
    }
    if response_schema is not None and profile.supports_structured_output:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "user_schema", "strict": True, "schema": response_schema},
        }
    return body


def _build_anthropic_body(profile: LLMProfile, prompt: PromptBundle,
                          response_schema: dict | None) -> dict:
    """POST {base_url}/v1/messages body. System messages fold into the top-level
    `system` param; images use source.type="base64"; structured output = a single
    forced tool named "emit" with the schema as input_schema (CONTRACTS.md §7.8)."""
    system_chunks: list[str] = []
    messages: list[dict] = []
    for msg in prompt.messages:
        if msg.role == "system":
            system_chunks.extend(part.text or "" for part in msg.parts if part.kind == "text")
            continue
        blocks: list[dict] = []
        for part in msg.parts:
            if part.kind == "text":
                blocks.append({"type": "text", "text": part.text or ""})
            else:
                assert part.image is not None
                media_type, b64 = part.image.load_base64(profile.max_image_px)
                blocks.append({"type": "image",
                               "source": {"type": "base64",
                                          "media_type": media_type,
                                          "data": b64}})
        messages.append({"role": msg.role, "content": blocks})
    body: dict = {
        "model": profile.model,
        "max_tokens": profile.max_output_tokens,
        "temperature": _resolve_temperature(profile, prompt),
        "messages": messages,
    }
    if system_chunks:
        body["system"] = "\n".join(system_chunks)
    if response_schema is not None and profile.supports_structured_output:
        body["tools"] = [{"name": STRUCTURED_TOOL_NAME, "input_schema": response_schema}]
        body["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
    return body


def _build_embeddings_body(profile: EmbeddingProfile, texts: list[str]) -> dict:
    """POST {base_url}/embeddings body (spec 3.9.3, v1.2)."""
    return {"model": profile.model, "input": list(texts)}


def _build_headers(provider: str, api_key: str) -> dict[str, str]:
    if provider == "anthropic":
        return {"x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json"}
    return {"Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"}


# ── pure helpers: response parsing ─────────────────────────────────────────

def _parse_anthropic_response(data: Mapping, fallback_model: str
                              ) -> tuple[str, dict | None, Usage, str]:
    """Extract (text, structured, usage, model) from a /v1/messages response.
    tool_use block (forced tool) → structured payload; text blocks joined for text."""
    texts: list[str] = []
    structured: dict | None = None
    for block in data.get("content") or ():
        if not isinstance(block, Mapping):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(str(block.get("text") or ""))
        elif btype == "tool_use" and structured is None:
            payload = block.get("input")
            if isinstance(payload, Mapping):
                structured = dict(payload)
    raw_usage = data.get("usage") or {}
    usage = Usage(prompt_tokens=int(raw_usage.get("input_tokens") or 0),
                  completion_tokens=int(raw_usage.get("output_tokens") or 0))
    model = str(data.get("model") or fallback_model)
    return "\n".join(texts), structured, usage, model


def _parse_openai_response(data: Mapping, fallback_model: str
                           ) -> tuple[str, dict | None, Usage, str]:
    """Extract (text, structured=None, usage, model) from a /chat/completions
    response. json_schema-mode output is TEXT — M9 never parses it (spec 3.9.1).
    Missing or unexpectedly-shaped bits degrade to defaults instead of raising,
    so a malformed 2xx never escapes M9 unclassified."""
    text = ""
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    message = first.get("message") if isinstance(first, Mapping) else None
    if isinstance(message, Mapping):
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):  # some gateways return typed part lists
            text = "".join(str(p.get("text") or "") for p in content if isinstance(p, Mapping))
    raw_usage = data.get("usage")
    if not isinstance(raw_usage, Mapping):
        raw_usage = {}
    usage = Usage(prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                  completion_tokens=int(raw_usage.get("completion_tokens") or 0))
    model = str(data.get("model") or fallback_model)
    return text, None, usage, model


def _parse_embeddings_response(data: Mapping, n_texts: int, profile_name: str,
                               dims: int | None) -> tuple[list[list[float]], Usage]:
    """Extract vectors aligned to input order; enforce the dims check
    (mismatch → ProviderFatalError, spec 3.9.2). Non-mapping items in ``data``
    are dropped rather than crashing — they then surface as a count mismatch
    (ProviderFatalError), never as an unclassified AttributeError."""
    raw_items = data.get("data")
    items = [it for it in (raw_items if isinstance(raw_items, (list, tuple)) else ())
             if isinstance(it, Mapping)]
    items.sort(key=lambda item: int(item.get("index") or 0))
    vectors = [[float(x) for x in (item.get("embedding") or ())] for item in items]
    if len(vectors) != n_texts:
        raise ProviderFatalError(
            f"embeddings response returned {len(vectors)} vectors for {n_texts} inputs",
            profile=profile_name, status_code=None)
    if dims is not None:
        for i, vec in enumerate(vectors):
            if len(vec) != dims:
                raise ProviderFatalError(
                    f"embedding dims mismatch at index {i}: expected {dims}, got {len(vec)}",
                    profile=profile_name, status_code=None)
    raw_usage = data.get("usage") or {}
    usage = Usage(prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
                  completion_tokens=int(raw_usage.get("completion_tokens") or 0))
    return vectors, usage


# ── pure helper: metering ──────────────────────────────────────────────────

def _accumulate_usage(acc: ProfileUsage, usage: Usage, retries: int,
                      price_per_mtok_in: float | None,
                      price_per_mtok_out: float | None) -> None:
    """One successful logical call: calls+1, token sums, retries; cost recomputed
    from the running totals when BOTH prices are configured (spec 3.9.3 计量)."""
    acc.calls += 1
    acc.prompt_tokens += usage.prompt_tokens
    acc.completion_tokens += usage.completion_tokens
    acc.retries += retries
    if price_per_mtok_in is not None and price_per_mtok_out is not None:
        acc.est_cost_usd = (acc.prompt_tokens / 1e6 * price_per_mtok_in
                            + acc.completion_tokens / 1e6 * price_per_mtok_out)


def _render_trace_messages(prompt: PromptBundle) -> list[dict]:
    """gen_ai.input.messages payload for trace.content='full'. Images are referenced
    by path, never inlined (base64 payloads do not belong in the trace)."""
    rendered: list[dict] = []
    for msg in prompt.messages:
        content: list[dict] = []
        for part in msg.parts:
            if part.kind == "text":
                content.append({"type": "text", "text": part.text or ""})
            else:
                content.append({"type": "image",
                                "path": str(part.image.path) if part.image else ""})
        rendered.append({"role": msg.role, "content": content})
    return rendered


def _render_output_messages(text: str, structured: dict | None) -> list[dict]:
    """gen_ai.output.messages payload for trace.content='full' (spec 7.4,
    CONTRACTS.md §8.2/§8.3): the assistant reply — native structured payload
    when present (anthropic forced tool), else the raw text."""
    return [{"role": "assistant",
             "content": structured if structured is not None else text}]


# ── the client ─────────────────────────────────────────────────────────────

class LLMClient:
    """Profile-keyed async provider client (CONTRACTS.md §7.8)."""

    def __init__(self, llm_profiles: Mapping[str, LLMProfile],
                 embedding_profiles: Mapping[str, EmbeddingProfile],
                 metrics: "MetricsSink | None" = None):
        self._llm_profiles: dict[str, LLMProfile] = dict(llm_profiles)
        self._embedding_profiles: dict[str, EmbeddingProfile] = dict(embedding_profiles)
        self._metrics = metrics
        self._usage: dict[str, ProfileUsage] = {}
        # One semaphore per profile, shared across ALL calls (incl. repairs,
        # verify, probe). Keyed by (kind, name) so an llm and an embedding
        # profile with the same name never share a limiter.
        self._semaphores: dict[tuple[str, str], asyncio.Semaphore] = {}
        # v1.6 key-pool state, keyed like the semaphores (in-memory only,
        # spec §2.6 — no persistence).
        self._pools: dict[tuple[str, str], _KeyPool] = {}
        # Jitter RNG is intentionally NOT seed-derived — timing only [FROZEN §7.8].
        self._jitter_rng = random.Random()
        self._http_client: httpx.AsyncClient | None = None

    # -- public API --------------------------------------------------------

    async def complete(self, profile: str, prompt: PromptBundle,
                       response_schema: dict | None = None) -> LLMResponse:
        """response_schema becomes L0 params only if the profile declares
        supports_structured_output, else it is ignored. Raises
        ProviderRetryableError (retries exhausted) / ProviderFatalError /
        CircuitBreakerTripped (fail-fast once the breaker is open)."""
        prof = self._llm_profiles.get(profile)
        if prof is None:
            raise ValueError(f"unknown [llm.*] profile: {profile!r}")
        self._check_breaker()

        if prof.provider == "anthropic":
            url = prof.base_url.rstrip("/") + "/v1/messages"
            build_body: Callable[[], dict] = lambda: _build_anthropic_body(
                prof, prompt, response_schema)
            parse = lambda data: _parse_anthropic_response(data, prof.model)
        else:
            url = prof.base_url.rstrip("/") + "/chat/completions"
            build_body = lambda: _build_openai_body(prof, prompt, response_schema)
            parse = lambda data: _parse_openai_response(data, prof.model)

        full_trace = self._full_content_trace_enabled()
        extra = {"gen_ai.input.messages": _render_trace_messages(prompt)} if full_trace else {}
        # On success the llm.call event must ALSO carry the response content at
        # trace.content="full" (spec 7.4 / CONTRACTS §8.2). The event is emitted
        # inside _post_with_retries, so the output messages are rendered there,
        # from the parse result, before the payload is serialized.
        finalize = (
            (lambda result: {"gen_ai.output.messages":
                             _render_output_messages(result[0], result[1])})
            if full_trace else None)

        (text, structured, usage, model), latency_ms, retries = await self._post_with_retries(
            kind="llm", prof=prof, url=url,
            build_body=build_body, parse=parse, trace_extra=extra,
            finalize_extra=finalize)

        _accumulate_usage(self._usage.setdefault(prof.name, ProfileUsage()),
                          usage, retries,
                          prof.price_per_mtok_in, prof.price_per_mtok_out)
        return LLMResponse(text=text, structured=structured, usage=usage,
                           model=model, latency_ms=latency_ms)

    async def embed(self, profile: str, texts: list[str]) -> list[list[float]]:
        """v1.2. profile must be an [embedding.*] name — [llm.*] names rejected
        (ValueError). openai_compatible only: POST {base_url}/embeddings; vectors
        aligned to input order; dims mismatch → ProviderFatalError. Usage metered
        under the embedding profile name; one llm.call trace event per call with
        operation="embedding". Retry/limit rules identical to complete()."""
        prof = self._embedding_profiles.get(profile)
        if prof is None:
            if profile in self._llm_profiles:
                raise ValueError(
                    f"embed() requires an [embedding.*] profile; {profile!r} is an [llm.*] name")
            raise ValueError(f"unknown [embedding.*] profile: {profile!r}")
        self._check_breaker()

        url = prof.base_url.rstrip("/") + "/embeddings"
        n = len(texts)
        (result,), _latency_ms, retries = await self._post_with_retries(
            kind="embedding", prof=prof, url=url,
            build_body=lambda: _build_embeddings_body(prof, texts),
            parse=lambda data: _split_embed(data, n, prof),
            operation="embedding")
        vectors, usage = result
        _accumulate_usage(self._usage.setdefault(prof.name, ProfileUsage()),
                          usage, retries, None, None)
        return vectors

    async def probe(self, profile: str) -> ProbeResult:
        """validate --probe: minimal 1-token live call (llm profiles) or 1-text
        embed (embedding profiles). Never raises; failures land in .error.
        Pooled profiles probe the FIRST key (v1.6) — probe_all covers the rest."""
        return (await self._probe_keys(profile, first_only=True))[0]

    async def probe_all(self, profile: str) -> list[ProbeResult]:
        """v1.6: one probe per pool key in declaration order, for llm AND
        embedding profiles — results carry key_env for pooled (>1 key)
        profiles. Single-key profiles degenerate to [await probe(profile)]
        with key_env=None. Used by ``validate --probe``. Never raises."""
        return await self._probe_keys(profile, first_only=False)

    async def _probe_keys(self, profile: str, *, first_only: bool) -> list[ProbeResult]:
        is_llm = profile in self._llm_profiles
        if not is_llm and profile not in self._embedding_profiles:
            return [ProbeResult(profile=profile, ok=False, model="", latency_ms=0,
                                error=f"unknown profile: {profile!r}")]
        prof = (self._llm_profiles[profile] if is_llm
                else self._embedding_profiles[profile])
        members = _pool_members(prof)
        pooled = len(members) > 1 and not first_only
        if first_only:
            members = members[:1]
        return [await self._probe_one(profile, prof, is_llm, env, key,
                                      key_env=env if pooled else None)
                for env, key in members]

    async def _probe_one(self, profile: str, prof, is_llm: bool,
                         env: str, key: str, *, key_env: str | None) -> ProbeResult:
        """Probe a single pool key via a throwaway sub-client whose profile is
        narrowed to that key (shares the connection pool and semaphores, so the
        per-profile aggregate concurrency cap still applies)."""
        start = time.monotonic()
        mod = replace(prof, api_key_env=env, api_key=key,
                      api_key_envs=(env,), api_keys=(key,))
        if is_llm:
            mod = replace(mod, max_output_tokens=1)
            client = LLMClient({profile: mod}, {}, self._metrics)
        else:
            client = LLMClient({}, {profile: mod}, self._metrics)
        client._http_client = self._http()  # share the connection pool
        client._semaphores = self._semaphores
        try:
            if is_llm:
                prompt = PromptBundle(messages=(
                    Message(role="user", parts=(Part(kind="text", text="ping"),)),))
                resp = await client.complete(profile, prompt)
                self._merge_usage(client._usage)
                return ProbeResult(profile=profile, ok=True, model=resp.model,
                                   latency_ms=resp.latency_ms, key_env=key_env)
            await client.embed(profile, ["ping"])
            self._merge_usage(client._usage)
            return ProbeResult(profile=profile, ok=True, model=prof.model,
                               latency_ms=int((time.monotonic() - start) * 1000),
                               key_env=key_env)
        except Exception as exc:  # noqa: BLE001 — probe never raises
            self._merge_usage(client._usage)
            return ProbeResult(profile=profile, ok=False, model=prof.model,
                               latency_ms=int((time.monotonic() - start) * 1000),
                               error=str(exc), key_env=key_env)

    @property
    def usage_by_profile(self) -> dict[str, ProfileUsage]:
        return self._usage

    async def aclose(self) -> None:
        """Release the shared httpx.AsyncClient (utility; call at run end)."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # -- internals ----------------------------------------------------------

    def _pool(self, kind: str, prof: LLMProfile | EmbeddingProfile) -> _KeyPool:
        key = (kind, prof.name)
        pool = self._pools.get(key)
        if pool is None:
            pool = _KeyPool(_pool_members(prof))
            self._pools[key] = pool
            if pool.size > 1:
                # Pre-seed one KeyUsage per member: report.llm_usage must list
                # EVERY key of a pooled profile (zeros for unused members) and
                # the keys-sub-object gate reflects pool SIZE, not traffic shape
                # — least-in-flight selection picks key 0 for serialized traffic,
                # which must not make a pool look single-key (§9.3, review fix).
                acc = self._usage.setdefault(prof.name, ProfileUsage())
                for s in pool.states:
                    acc.keys.setdefault(s.env, KeyUsage())
        return pool

    def _max_park_s(self) -> float:
        """run.max_park_s (v1.6, spec 5.2) via the metrics sink's cfg; default
        3600 for directly-constructed clients (tests, probe children)."""
        cfg = getattr(self._metrics, "cfg", None)
        run = getattr(cfg, "run", None)
        return float(getattr(run, "max_park_s", 3600))

    def _semaphore(self, kind: str, name: str, max_concurrency: int) -> asyncio.Semaphore:
        key = (kind, name)
        sem = self._semaphores.get(key)
        if sem is None:
            sem = asyncio.Semaphore(max_concurrency)
            self._semaphores[key] = sem
        return sem

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=None)
        return self._http_client

    def _check_breaker(self) -> None:
        if self._metrics is not None and getattr(self._metrics, "circuit_broken", False):
            raise CircuitBreakerTripped("provider circuit breaker is open")

    def _full_content_trace_enabled(self) -> bool:
        cfg = getattr(self._metrics, "cfg", None)
        trace = getattr(cfg, "trace", None)
        if trace is None:
            return False
        return (getattr(trace, "enabled", False)
                and getattr(trace, "content", "") == "full"
                and "llm" in (getattr(trace, "channels", ()) or ()))

    def _record_provider_result(self, fatal: bool, *, hard: bool = False) -> None:
        if self._metrics is not None:
            record = getattr(self._metrics, "record_provider_result", None)
            if record is not None:
                try:
                    record(fatal=fatal, hard=hard)
                except TypeError:      # older/minimal sinks without the hard kwarg
                    record(fatal=fatal)

    def _emit_llm_call(self, prof: LLMProfile | EmbeddingProfile, *, latency_ms: int,
                       usage: Usage, retries: int, status: str,
                       operation: str | None, extra: Mapping | None = None) -> None:
        if self._metrics is None:
            return
        payload: dict = {
            "profile": prof.name,
            "gen_ai.request.model": prof.model,
            "latency_ms": latency_ms,
            "gen_ai.usage.input_tokens": usage.prompt_tokens,
            "gen_ai.usage.output_tokens": usage.completion_tokens,
            "retries": retries,
            "status": status,
        }
        if operation is not None:
            payload["operation"] = operation
        if extra:
            payload.update(extra)
        emit = getattr(self._metrics, "event", None)
        if emit is not None:
            emit(EV_LLM_CALL, stage="llm", batch_no=0, record_ids=(), payload=payload)

    def _merge_usage(self, other: dict[str, ProfileUsage]) -> None:
        for name, src in other.items():
            acc = self._usage.setdefault(name, ProfileUsage())
            acc.calls += src.calls
            acc.prompt_tokens += src.prompt_tokens
            acc.completion_tokens += src.completion_tokens
            acc.retries += src.retries
            if src.est_cost_usd is not None:
                acc.est_cost_usd = (acc.est_cost_usd or 0.0) + src.est_cost_usd
            acc.parked_calls += src.parked_calls
            acc.parked_ms += src.parked_ms
            for env, ku in src.keys.items():
                dst = acc.keys.setdefault(env, KeyUsage())
                dst.calls += ku.calls
                dst.rate_limited += ku.rate_limited
                dst.disabled = dst.disabled or ku.disabled

    def _emit_event(self, ev: str, payload: dict) -> None:
        """Emit a v1.6 key-pool event (llm.key_cooldown / llm.key_disabled /
        llm.pool_parked). Payloads carry env-var NAMES only — key values never
        enter any log path (spec 7.2/7.4)."""
        if self._metrics is None:
            return
        emit = getattr(self._metrics, "event", None)
        if emit is not None:
            emit(ev, stage="llm", batch_no=0, record_ids=(), payload=payload)

    async def _post_with_retries(self, *, kind: str,
                                 prof: LLMProfile | EmbeddingProfile,
                                 url: str,
                                 build_body: Callable[[], dict],
                                 parse: Callable[[Mapping], tuple],
                                 operation: str | None = None,
                                 trace_extra: Mapping | None = None,
                                 finalize_extra: Callable[[tuple], Mapping] | None = None,
                                 ) -> tuple[tuple, int, int]:
        """Shared engine: semaphore → per-attempt key selection (v1.6 pool) →
        attempt loop → metering hooks + llm.call trace on every outcome. Returns
        (parse(...) result tuple, latency_ms of the final attempt, retries used).
        ``finalize_extra`` (success only) renders extra trace payload fields from
        the parse result — e.g. gen_ai.output.messages at trace.content="full" —
        merged over ``trace_extra`` before the llm.call event is emitted.

        v1.6 key pool (spec 3.9.3 密钥池行): headers are built PER ATTEMPT from
        the least-in-flight eligible key. A 429 cools THAT KEY (Retry-After in
        full, else full-jitter capped 300 s on the key's cross-call 429 counter)
        and the next attempt rotates immediately — zero wait while another key
        is live. 401/403 disables the key: absorbed silently while siblings
        live (no retry consumed, nothing fed to the breaker); disabling the
        LAST live key hard-trips, preserving v1.5 semantics for pools of 1.
        All live keys cooling → park inside the held semaphore slot, in ≤ 60 s
        slices with a breaker re-check per slice, bounded per logical call by
        run.max_park_s — overrun takes the retry-exhaustion path (P1-1).
        400/404 and fatal-parse stay key-independent: no rotation, immediate
        fatal, exactly as v1.5."""
        sem = self._semaphore(kind, prof.name, prof.max_concurrency)
        pool = self._pool(kind, prof)
        pooled = pool.size > 1
        acc = self._usage.setdefault(prof.name, ProfileUsage())
        client = self._http()
        retries_used = 0
        latency_ms = 0
        park_budget = self._max_park_s()
        park_spent = 0.0
        parked_this_call = False
        last_env: str | None = None

        def key_extra() -> Mapping | None:
            # llm.call payload + key_env (pools > 1 only): env-var NAME of the
            # key used by the LAST attempt; absent on zero-attempt calls.
            if pooled and last_env is not None:
                merged = dict(trace_extra or {})
                merged["key_env"] = last_env
                return merged
            return trace_extra

        async with sem:
            attempt = 0
            while True:
                # Re-check the breaker per attempt, AFTER semaphore acquisition:
                # under gather() every queued coroutine passes the complete()
                # entry check before any HTTP finishes, so without this a call
                # queued behind the one that tripped the breaker would still
                # fire a doomed request (fail-fast contract, spec 3.9.2).
                try:
                    self._check_breaker()
                except CircuitBreakerTripped:
                    if retries_used:
                        # This logical call DID hit the wire before the breaker
                        # opened mid-backoff — its attempts must not vanish from
                        # report.llm_usage / the llm.call trace (review finding).
                        acc.retries += retries_used
                        self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                            retries=retries_used,
                                            status="breaker_aborted",
                                            operation=operation, extra=key_extra())
                    raise

                now = time.monotonic()
                ks = pool.select(now)
                if ks is None:
                    live = pool.live()
                    if not live:
                        # Whole pool auth-dead. The call that disabled the last
                        # key already hard-tripped the breaker, so the entry /
                        # per-attempt checks normally catch this first —
                        # defensive terminal fatal (feeds the streak).
                        self._record_provider_result(fatal=True)
                        if retries_used:
                            acc.retries += retries_used
                        self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                            retries=retries_used, status="fatal",
                                            operation=operation, extra=key_extra())
                        raise ProviderFatalError(
                            "all keys of the pool are auth-disabled",
                            profile=prof.name, key_env=last_env)
                    wait = pool.earliest_wake(now)
                    if park_budget <= 0 or park_spent + wait > park_budget:
                        # Park overrun — incl. the provably-hopeless case (the
                        # earliest cooldown end exceeds the remaining budget:
                        # fail now, no dead wall-clock) and max_park_s = 0 (no
                        # parking). Retry-exhaustion path: record failed, feeds
                        # the breaker window (spec 3.9.3, 1.6 decision ③).
                        self._record_provider_result(fatal=True)
                        if retries_used:
                            acc.retries += retries_used
                        self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                            retries=retries_used,
                                            status="retryable_exhausted",
                                            operation=operation, extra=key_extra())
                        raise ProviderRetryableError(
                            f"park budget exhausted ({park_spent:.0f}s parked, next "
                            f"key eligible in {wait:.0f}s, run.max_park_s="
                            f"{park_budget:.0f}): all live keys cooling",
                            profile=prof.name, retries=retries_used, key_env=last_env)
                    if not parked_this_call:
                        parked_this_call = True
                        acc.parked_calls += 1
                    self._emit_event(EV_LLM_POOL_PARKED,
                                     {"profile": prof.name, "wait_s": round(wait, 3),
                                      "live_keys": len(live)})
                    end = now + wait
                    while True:
                        # Breaker re-check per ≤60s slice (v1.5 hardening kept):
                        # an open breaker breaks out; the loop top then raises
                        # CircuitBreakerTripped with breaker_aborted accounting.
                        if self._metrics is not None and getattr(
                                self._metrics, "circuit_broken", False):
                            break
                        remaining = end - time.monotonic()
                        if remaining <= 0:
                            break
                        await asyncio.sleep(min(_PARK_SLICE_S, remaining))
                    elapsed = time.monotonic() - now
                    park_spent += elapsed
                    acc.parked_ms += int(elapsed * 1000)
                    continue

                last_env = ks.env
                ku = acc.keys.setdefault(ks.env, KeyUsage())
                headers = _build_headers(prof.provider, ks.key)

                failure_msg = ""
                status_code: int | None = None
                retry_after: float | None = None
                retryable = True
                # Image bytes are loaded HERE, per attempt, and released with `body`
                # right after the request completes (lazy-load contract, spec §2.6).
                body = build_body()
                start = time.monotonic()
                ks.in_flight += 1
                try:
                    resp = await client.post(url, json=body, headers=headers,
                                             timeout=httpx.Timeout(prof.timeout_s))
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    failure_msg = f"{type(exc).__name__}: {exc}"
                else:
                    latency_ms = int((time.monotonic() - start) * 1000)
                    if 200 <= resp.status_code < 300:
                        try:
                            data = resp.json()
                        except (ValueError, json.JSONDecodeError):
                            failure_msg = "provider returned unparseable JSON"
                        else:
                            try:
                                result = parse(data)
                            except ProviderFatalError:
                                self._record_provider_result(fatal=True)
                                if retries_used:
                                    acc.retries += retries_used
                                self._emit_llm_call(prof, latency_ms=latency_ms,
                                                    usage=Usage(), retries=retries_used,
                                                    status="fatal", operation=operation,
                                                    extra=key_extra())
                                raise
                            except Exception as exc:  # noqa: BLE001 — malformed 2xx body
                                # A 2xx whose JSON parses but has an unexpected
                                # shape is the same class of provider fault as an
                                # unparseable body: retryable, never allowed to
                                # escape M9 unclassified (spec 3.9.2 / §7.8).
                                failure_msg = ("malformed provider response: "
                                               f"{type(exc).__name__}: {exc}")
                            else:
                                usage = _result_usage(result)
                                success_extra: Mapping | None = key_extra()
                                if finalize_extra is not None:
                                    success_extra = dict(success_extra or {})
                                    success_extra.update(finalize_extra(result))
                                ks.consec_429 = 0     # per-key success resets c
                                ku.calls += 1
                                self._record_provider_result(fatal=False)
                                self._emit_llm_call(prof, latency_ms=latency_ms, usage=usage,
                                                    retries=retries_used, status="ok",
                                                    operation=operation, extra=success_extra)
                                return result, latency_ms, retries_used
                    else:
                        status_code = resp.status_code
                        retryable = _is_retryable_status(status_code)
                        failure_msg = f"HTTP {status_code}: {resp.text[:300]}"
                        if status_code == 429:
                            retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                finally:
                    ks.in_flight -= 1
                    del body   # release request bytes (incl. base64 images)

                if status_code in (401, 403):
                    # Auth is a KEY-level deterministic failure (v1.6): disable
                    # the key for the run. Concurrent in-flight calls can all
                    # observe the same key's 401 — the event/WARN fires at most
                    # once per key per run (spec 7.2 cardinality); every
                    # observer still rotates or hard-trips below.
                    if not ks.disabled:
                        ks.disabled = True
                        ku.disabled = True
                        self._emit_event(EV_LLM_KEY_DISABLED,
                                         {"profile": prof.name, "key_env": ks.env,
                                          "status_code": status_code})
                    if pool.live():
                        # Absorbed: the SAME attempt re-dispatches on the next
                        # key — no retry budget consumed, nothing fed to the
                        # breaker (each key can auth-fail at most once, so the
                        # rotation is bounded by the pool size).
                        continue
                    # Last live key disabled → v1.5 auth semantics: immediate
                    # hard trip (credentials never self-heal, spec 3.9.3).
                    self._record_provider_result(fatal=True, hard=True)
                    if retries_used:
                        acc.retries += retries_used
                    self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                        retries=retries_used, status="fatal",
                                        operation=operation, extra=key_extra())
                    raise ProviderFatalError(failure_msg, profile=prof.name,
                                             status_code=status_code, key_env=ks.env)
                if not retryable:
                    # 400/404: request-shape errors are key-independent — no
                    # rotation, immediate fatal feeding the streak (spec 3.9.3).
                    self._record_provider_result(fatal=True)
                    if retries_used:
                        acc.retries += retries_used
                    self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                        retries=retries_used, status="fatal",
                                        operation=operation, extra=key_extra())
                    raise ProviderFatalError(failure_msg, profile=prof.name,
                                             status_code=status_code, key_env=ks.env)
                if status_code == 429:
                    # v1.6: ALL 429 waiting is expressed as per-key cooldown —
                    # Retry-After honored in full, else full-jitter capped 300 s
                    # on the key's cross-call consecutive-429 counter.
                    ks.consec_429 += 1
                    ku.rate_limited += 1
                    cooldown = (retry_after if retry_after is not None
                                else self._jitter_rng.uniform(
                                    0.0, _key_cooldown_upper(prof.retry_base_delay_s,
                                                             ks.consec_429)))
                    ks.cooldown_until = time.monotonic() + cooldown
                    self._emit_event(EV_LLM_KEY_COOLDOWN,
                                     {"profile": prof.name, "key_env": ks.env,
                                      "cooldown_s": round(cooldown, 3),
                                      "retry_after": retry_after is not None})
                if attempt >= prof.max_retries:
                    # Retry exhaustion counts toward the breaker window too
                    # (spec 7.6 provider_retryable_exhausted:「计入熔断窗口」).
                    self._record_provider_result(fatal=True)
                    if retries_used:
                        acc.retries += retries_used
                    self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                        retries=retries_used, status="retryable_exhausted",
                                        operation=operation, extra=key_extra())
                    raise ProviderRetryableError(
                        f"retries exhausted ({retries_used}): {failure_msg}",
                        profile=prof.name, retries=retries_used, key_env=ks.env)
                attempt += 1
                retries_used += 1
                if status_code == 429:
                    # No inline sleep: the 429 wait lives on the key's cooldown —
                    # the next attempt rotates to a live key or parks (v1.6).
                    continue
                wait = _backoff_delay(attempt, prof.retry_base_delay_s, self._jitter_rng)
                await asyncio.sleep(wait)


def _split_embed(data: Mapping, n: int, prof: EmbeddingProfile) -> tuple:
    """Adapter so the retry engine can meter embedding usage uniformly."""
    vectors, usage = _parse_embeddings_response(data, n, prof.name, prof.dims)
    return ((vectors, usage),)


def _result_usage(result: tuple) -> Usage:
    """Pull the Usage out of a parse() result tuple (complete: 4-tuple with usage
    at index 2; embed: 1-tuple of (vectors, usage))."""
    if len(result) == 4:
        return result[2]
    inner = result[0]
    return inner[1]

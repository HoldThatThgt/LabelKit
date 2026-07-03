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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping

import httpx

from labelkit.config.model import EmbeddingProfile, LLMProfile
from labelkit.errors import (
    CircuitBreakerTripped,
    ProviderFatalError,
    ProviderRetryableError,
)
from labelkit.types import ImageRef, Usage

if TYPE_CHECKING:
    from labelkit.obslog import MetricsSink

from labelkit.obslog import EV_LLM_CALL

ANTHROPIC_VERSION = "2023-06-01"          # [FROZEN in CONTRACTS.md §7.8]
STRUCTURED_TOOL_NAME = "emit"             # [FROZEN in CONTRACTS.md §7.8]
_MAX_BACKOFF_S = 60.0                     # backoff cap (spec 3.9.3)


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


@dataclass                                          # mutable per-profile accumulator
class ProfileUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retries: int = 0
    est_cost_usd: float | None = None              # only when prices configured


@dataclass(frozen=True)
class ProbeResult:
    profile: str
    ok: bool
    model: str
    latency_ms: int
    error: str | None = None


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

        headers = _build_headers(prof.provider, self._api_key(prof))
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
            kind="llm", prof=prof, url=url, headers=headers,
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
        headers = _build_headers(prof.provider, self._api_key(prof))
        n = len(texts)
        (result,), _latency_ms, retries = await self._post_with_retries(
            kind="embedding", prof=prof, url=url, headers=headers,
            build_body=lambda: _build_embeddings_body(prof, texts),
            parse=lambda data: _split_embed(data, n, prof),
            operation="embedding")
        vectors, usage = result
        _accumulate_usage(self._usage.setdefault(prof.name, ProfileUsage()),
                          usage, retries, None, None)
        return vectors

    async def probe(self, profile: str) -> ProbeResult:
        """validate --probe: minimal 1-token live call (llm profiles) or 1-text
        embed (embedding profiles). Never raises; failures land in .error."""
        start = time.monotonic()
        if profile in self._llm_profiles:
            prof = self._llm_profiles[profile]
            probe_prof = replace(prof, max_output_tokens=1)
            client = LLMClient({profile: probe_prof}, {}, self._metrics)
            client._http_client = self._http()  # share the connection pool
            client._semaphores = self._semaphores
            try:
                prompt = PromptBundle(messages=(
                    Message(role="user", parts=(Part(kind="text", text="ping"),)),))
                resp = await client.complete(profile, prompt)
                self._merge_usage(client._usage)
                return ProbeResult(profile=profile, ok=True, model=resp.model,
                                   latency_ms=resp.latency_ms)
            except Exception as exc:  # noqa: BLE001 — probe never raises
                self._merge_usage(client._usage)
                return ProbeResult(profile=profile, ok=False, model=prof.model,
                                   latency_ms=int((time.monotonic() - start) * 1000),
                                   error=str(exc))
        if profile in self._embedding_profiles:
            prof_e = self._embedding_profiles[profile]
            try:
                await self.embed(profile, ["ping"])
                return ProbeResult(profile=profile, ok=True, model=prof_e.model,
                                   latency_ms=int((time.monotonic() - start) * 1000))
            except Exception as exc:  # noqa: BLE001
                return ProbeResult(profile=profile, ok=False, model=prof_e.model,
                                   latency_ms=int((time.monotonic() - start) * 1000),
                                   error=str(exc))
        return ProbeResult(profile=profile, ok=False, model="", latency_ms=0,
                           error=f"unknown profile: {profile!r}")

    @property
    def usage_by_profile(self) -> dict[str, ProfileUsage]:
        return self._usage

    async def aclose(self) -> None:
        """Release the shared httpx.AsyncClient (utility; call at run end)."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # -- internals ----------------------------------------------------------

    def _api_key(self, prof: LLMProfile | EmbeddingProfile) -> str:
        # M1 resolves api_key from api_key_env; fall back to the env var for
        # directly-constructed profiles (never logged anywhere).
        return prof.api_key or os.environ.get(prof.api_key_env, "")

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

    async def _post_with_retries(self, *, kind: str,
                                 prof: LLMProfile | EmbeddingProfile,
                                 url: str, headers: dict[str, str],
                                 build_body: Callable[[], dict],
                                 parse: Callable[[Mapping], tuple],
                                 operation: str | None = None,
                                 trace_extra: Mapping | None = None,
                                 finalize_extra: Callable[[tuple], Mapping] | None = None,
                                 ) -> tuple[tuple, int, int]:
        """Shared engine: semaphore → attempt loop with full-jitter backoff →
        metering hooks + llm.call trace on every outcome. Returns
        (parse(...) result tuple, latency_ms of the final attempt, retries used).
        ``finalize_extra`` (success only) renders extra trace payload fields from
        the parse result — e.g. gen_ai.output.messages at trace.content="full" —
        merged over ``trace_extra`` before the llm.call event is emitted."""
        sem = self._semaphore(kind, prof.name, prof.max_concurrency)
        client = self._http()
        retries_used = 0
        latency_ms = 0
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
                        self._usage.setdefault(prof.name, ProfileUsage()
                                               ).retries += retries_used
                        self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                            retries=retries_used,
                                            status="breaker_aborted",
                                            operation=operation, extra=trace_extra)
                    raise
                failure_msg = ""
                status_code: int | None = None
                retry_after: float | None = None
                retryable = True
                # Image bytes are loaded HERE, per attempt, and released with `body`
                # right after the request completes (lazy-load contract, spec §2.6).
                body = build_body()
                start = time.monotonic()
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
                                    self._usage.setdefault(prof.name, ProfileUsage()
                                                           ).retries += retries_used
                                self._emit_llm_call(prof, latency_ms=latency_ms,
                                                    usage=Usage(), retries=retries_used,
                                                    status="fatal", operation=operation,
                                                    extra=trace_extra)
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
                                success_extra: Mapping | None = trace_extra
                                if finalize_extra is not None:
                                    success_extra = dict(trace_extra or {})
                                    success_extra.update(finalize_extra(result))
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
                    del body   # release request bytes (incl. base64 images)

                if not retryable:
                    # 401/403 are credential/permission failures: they will fail
                    # identically on every subsequent call, so trip the breaker
                    # immediately instead of counting a streak (spec 3.9.3).
                    self._record_provider_result(fatal=True,
                                                 hard=status_code in (401, 403))
                    if retries_used:
                        self._usage.setdefault(prof.name, ProfileUsage()).retries += retries_used
                    self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                        retries=retries_used, status="fatal",
                                        operation=operation, extra=trace_extra)
                    raise ProviderFatalError(failure_msg, profile=prof.name,
                                             status_code=status_code)
                if attempt >= prof.max_retries:
                    # Retry exhaustion counts toward the breaker window too
                    # (spec 7.6 provider_retryable_exhausted:「计入熔断窗口」).
                    self._record_provider_result(fatal=True)
                    if retries_used:
                        self._usage.setdefault(prof.name, ProfileUsage()).retries += retries_used
                    self._emit_llm_call(prof, latency_ms=latency_ms, usage=Usage(),
                                        retries=retries_used, status="retryable_exhausted",
                                        operation=operation, extra=trace_extra)
                    raise ProviderRetryableError(
                        f"retries exhausted ({retries_used}): {failure_msg}",
                        profile=prof.name, retries=retries_used)
                attempt += 1
                retries_used += 1
                if retry_after is not None:            # Retry-After takes precedence on 429
                    wait = retry_after
                else:
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

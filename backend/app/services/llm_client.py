"""Provider-agnostic LLM client.

Supports Anthropic and OpenRouter over plain HTTP (httpx) — no heavy SDK — plus
a no-network "stub" used when no credentials are configured so the app still
runs in dev/CI. Adds a request timeout and simple exponential-backoff retry on
transient failures (network errors, 429, 5xx).

Public surface:
    await complete(system, prompt, max_tokens=None, temperature=None) -> str
"""

import asyncio
import json
from typing import AsyncIterator

import httpx

from app.config import settings
from app.logging_config import get_logger

logger = get_logger("llm")

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Non-retryable LLM failure (bad request, auth, exhausted retries)."""


class _RetryableError(Exception):
    """Transient failure worth retrying (timeout, connection error, 5xx/429)."""


def _resolve_provider() -> str:
    provider = (settings.llm_provider or "auto").lower()
    if provider == "auto":
        if settings.anthropic_api_key:
            return "anthropic"
        if settings.openrouter_api_key:
            return "openrouter"
        return "stub"
    return provider


async def _post(url: str, headers: dict, payload: dict) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            response = await client.post(url, headers=headers, json=payload)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _RetryableError(f"transport error: {exc}") from exc

    if response.status_code in _RETRYABLE_STATUS:
        raise _RetryableError(f"status {response.status_code}: {response.text[:200]}")
    if response.status_code >= 400:
        raise LLMError(f"LLM request failed [{response.status_code}]: {response.text[:300]}")
    return response


async def _anthropic_complete(system: str, prompt: str, max_tokens: int, temperature: float) -> str:
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = await _post(_ANTHROPIC_URL, headers, payload)
    data = response.json()
    parts = [
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(parts).strip()


async def _openrouter_complete(system: str, prompt: str, max_tokens: int, temperature: float) -> str:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    response = await _post(_OPENROUTER_URL, headers, payload)
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


async def _stub_complete(system: str, prompt: str, max_tokens: int, temperature: float) -> str:
    # No network. Deterministic echo so the pipeline is observable without keys.
    return f"[stub-llm] {prompt[:400]}".strip()


_PROVIDERS = {
    "anthropic": _anthropic_complete,
    "openrouter": _openrouter_complete,
    "stub": _stub_complete,
}


async def complete(
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    provider = _resolve_provider()
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise LLMError(f"unknown LLM provider: {provider}")

    if provider == "anthropic" and not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    if provider == "openrouter" and not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")

    max_tokens = max_tokens or settings.llm_max_tokens
    temperature = settings.llm_temperature if temperature is None else temperature

    attempts = max(1, settings.llm_max_retries + 1)
    delay = 0.5
    for attempt in range(1, attempts + 1):
        try:
            result = await fn(system, prompt, max_tokens, temperature)
            logger.info(
                "llm.completed",
                extra={"provider": provider, "model": settings.llm_model, "attempt": attempt},
            )
            return result
        except _RetryableError as exc:
            if attempt < attempts:
                logger.warning(
                    "llm.retry",
                    extra={"provider": provider, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error("llm.failed", extra={"provider": provider, "error": str(exc)})
                raise LLMError(f"LLM failed after {attempts} attempt(s): {exc}") from exc


# ---------------------------------------------------------------------------
# Streaming (Phase 5)
# ---------------------------------------------------------------------------

async def _raise_for_stream_status(response: httpx.Response) -> None:
    if response.status_code in _RETRYABLE_STATUS:
        body = (await response.aread()).decode(errors="replace")
        raise _RetryableError(f"status {response.status_code}: {body[:200]}")
    if response.status_code >= 400:
        body = (await response.aread()).decode(errors="replace")
        raise LLMError(f"stream failed [{response.status_code}]: {body[:300]}")


def _parse_anthropic_line(line: str) -> str | None:
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload:
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta", {})
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return None


def _parse_openai_line(line: str) -> str | None:
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    choices = obj.get("choices", [])
    if choices:
        return choices[0].get("delta", {}).get("content")
    return None


async def _stream_http(url, headers, payload, parse_line) -> AsyncIterator[str]:
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                await _raise_for_stream_status(response)
                async for line in response.aiter_lines():
                    text = parse_line(line)
                    if text:
                        yield text
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise _RetryableError(f"transport error: {exc}") from exc


async def _stream_anthropic(system, prompt, max_tokens, temperature) -> AsyncIterator[str]:
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    async for chunk in _stream_http(_ANTHROPIC_URL, headers, payload, _parse_anthropic_line):
        yield chunk


async def _stream_openrouter(system, prompt, max_tokens, temperature) -> AsyncIterator[str]:
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "content-type": "application/json",
    }
    payload = {
        "model": settings.llm_model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
    }
    async for chunk in _stream_http(_OPENROUTER_URL, headers, payload, _parse_openai_line):
        yield chunk


async def _stream_stub(system, prompt, max_tokens, temperature) -> AsyncIterator[str]:
    text = f"[stub-llm] {prompt[:400]}".strip()
    for i in range(0, len(text), 12):
        yield text[i : i + 12]


_STREAM_PROVIDERS = {
    "anthropic": _stream_anthropic,
    "openrouter": _stream_openrouter,
    "stub": _stream_stub,
}


async def stream(
    system: str,
    prompt: str,
    *,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> AsyncIterator[str]:
    """Yield answer text incrementally.

    Retries transient failures only *before the first token* is emitted; once
    streaming has begun a mid-stream failure surfaces as LLMError (we can't
    safely restart a partially-consumed stream).
    """
    provider = _resolve_provider()
    fn = _STREAM_PROVIDERS.get(provider)
    if fn is None:
        raise LLMError(f"unknown LLM provider: {provider}")
    if provider == "anthropic" and not settings.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    if provider == "openrouter" and not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")

    max_tokens = max_tokens or settings.llm_max_tokens
    temperature = settings.llm_temperature if temperature is None else temperature

    attempts = max(1, settings.llm_max_retries + 1)
    delay = 0.5
    for attempt in range(1, attempts + 1):
        started = False
        try:
            async for chunk in fn(system, prompt, max_tokens, temperature):
                started = True
                yield chunk
            logger.info("llm.stream_completed", extra={"provider": provider, "attempt": attempt})
            return
        except _RetryableError as exc:
            if started:
                logger.error("llm.stream_interrupted", extra={"provider": provider, "error": str(exc)})
                raise LLMError(f"stream interrupted: {exc}") from exc
            if attempt < attempts:
                logger.warning("llm.stream_retry", extra={"provider": provider, "attempt": attempt})
                await asyncio.sleep(delay)
                delay *= 2
            else:
                logger.error("llm.stream_failed", extra={"provider": provider, "error": str(exc)})
                raise LLMError(f"stream failed after {attempts} attempt(s): {exc}") from exc

"""Provider-agnostic LLM client.

Supports Anthropic and OpenRouter over plain HTTP (httpx) — no heavy SDK — plus
a no-network "stub" used when no credentials are configured so the app still
runs in dev/CI. Adds a request timeout and simple exponential-backoff retry on
transient failures (network errors, 429, 5xx).

Public surface:
    await complete(system, prompt, max_tokens=None, temperature=None) -> str
"""

import asyncio

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

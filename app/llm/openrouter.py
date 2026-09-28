"""OpenRouter client: the single place the `native` backend talks to a model.

Scope: one chat completion call with usage parsing. It does NOT choose models, build prompts,
or record runs — the worker does that. Owner: platform.

Why OpenRouter and not each provider directly: provider-agnosticism is a product requirement
(model per agent, any provider). OpenRouter is the OpenAI-compatible gateway, so this file
speaks plain OpenAI chat-completions and works for every model on it.

Cost comes from the provider's own `usage.cost` (USD credits), never from a local price table.
A price table rots; the provider's number does not. BRL conversion uses USD_BRL_ESTIMATE, which
is marked everywhere it appears — a conversion assumption, not a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Documented assumption, not a measurement. Shown with ~ wherever it is displayed.
USD_BRL_ESTIMATE = 5.5


@dataclass
class LlmResult:
    """Everything a completed call returns. `ok=False` means transport-level failure (timeout,
    5xx, malformed envelope) — a model that answered badly but validly is `ok=True` with its
    text, because quality judgement belongs to verification, not to transport."""

    ok: bool
    text: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float | None = None
    provider_request_id: str = ""
    latency_ms: int = 0
    error: str = ""
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_brl(self) -> float:
        """BRL through the documented conversion. None-source stays None upstream; here the
        column default (0) applies only when nothing was reported — and the UI must say so."""
        if self.cost_usd is None:
            return 0.0
        return round(self.cost_usd * USD_BRL_ESTIMATE, 6)


async def chat(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None = None,
    temperature: float = 0.7,
    timeout_s: float = 120.0,
    client: httpx.AsyncClient | None = None,
    extra_headers: dict[str, str] | None = None,
) -> LlmResult:
    """POST one chat completion. Pure transport: no retries here (retry policy belongs to the
    worker, which knows the failure matrix), no model selection, no prompt building."""
    import time

    if not api_key:
        return LlmResult(ok=False, error="missing OPENROUTER_API_KEY")
    if not model:
        return LlmResult(ok=False, error="missing model")

    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://agentos.local",
        "X-Title": "AgentOS native backend",
    }
    if extra_headers:
        headers.update(extra_headers)

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout_s)
    started = time.monotonic()
    try:
        response = await client.post(OPENROUTER_URL, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001 - transport failure is a normal result here
        return LlmResult(ok=False, error=f"transport: {exc}")
    finally:
        if own_client:
            await client.aclose()
    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code == 429:
        return LlmResult(ok=False, error="rate_limited", latency_ms=latency_ms)
    if response.status_code >= 500:
        return LlmResult(
            ok=False, error=f"provider_5xx:{response.status_code}", latency_ms=latency_ms
        )
    if response.status_code != 200:
        return LlmResult(
            ok=False,
            error=f"provider_{response.status_code}:{response.text[:200]}",
            latency_ms=latency_ms,
        )

    try:
        envelope = response.json()
    except Exception as exc:  # noqa: BLE001
        return LlmResult(ok=False, error=f"bad_envelope: {exc}", latency_ms=latency_ms)

    try:
        choice = (envelope.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "").strip()
        usage = envelope.get("usage") or {}
        return LlmResult(
            ok=True,
            text=text,
            model=envelope.get("model") or model,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(
                usage.get("cached_tokens")
                or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                or 0
            ),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            cost_usd=(
                float(usage["cost"]) if usage.get("cost") is not None else None
            ),
            provider_request_id=str(envelope.get("id") or ""),
            latency_ms=latency_ms,
            raw_usage=usage,
        )
    except Exception as exc:  # noqa: BLE001 - envelope shape drift is a normal failure
        return LlmResult(ok=False, error=f"bad_choice: {exc}", latency_ms=latency_ms)

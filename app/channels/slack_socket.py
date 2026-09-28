"""Slack Socket Mode listener: inbound events without public ingress.

Scope: hold one outbound websocket to Slack, translate message/app_mention events into
normalised intake dicts, hand them to a caller-provided async handler. It does NOT route,
execute, or reply — the handler (wired in Wave 2+) owns the pipeline.

Owner: platform.

Why Socket Mode and not Events API webhooks: this host has no public TLS ingress (no proxy
on the Coolify server), and webhooks REQUIRE one. Socket Mode is a single outbound websocket,
so it works behind NAT with zero firewall changes. The tradeoff is a long-lived connection
the worker must supervise (reconnect with backoff, which slack-sdk already does).

Requires the `slack-sdk` package with an app-level token (xapp-*) for the socket plus the bot
token (xoxb-*) for API calls. Without tokens the listener refuses to start loudly — a bot
that silently receives nothing is worse than one that refuses to boot.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class SlackSocketNotConfigured(RuntimeError):
    """Raised at startup when tokens are missing. See slack_api.SlackNotConfigured for why
    loud beats silent here."""


def require_tokens(*, app_token: str, bot_token: str) -> tuple[str, str]:
    """Validate both tokens. Socket Mode needs TWO: xapp-* holds the websocket, xoxb-* makes
    the API calls. Mixing them up is the classic first-hour failure, so the prefixes are
    checked, not just presence."""
    app_token, bot_token = (app_token or "").strip(), (bot_token or "").strip()
    if not app_token.startswith("xapp-"):
        raise SlackSocketNotConfigured(
            "SLACK_APP_TOKEN ausente ou inválido (esperado xapp-*): ative Socket Mode no app "
            "em api.slack.com e gere o token em Basic Information > App-Level Tokens."
        )
    if not bot_token.startswith("xoxb-"):
        raise SlackSocketNotConfigured(
            "SLACK_BOT_TOKEN ausente ou inválido (esperado xoxb-*): instale o app no workspace "
            "em OAuth & Permissions."
        )
    return app_token, bot_token


def normalise_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate a Socket Mode envelope/message into intake shape. Returns None for anything
    that is not human text worth routing: bot messages (our own echoes would loop), edits,
    joins, reactions. Pure function, tested per branch — an intake filter with a bug either
    drops real messages or, worse, loops on its own replies."""
    inner = event.get("event") or {}
    if event.get("type") != "events_api":
        return None
    if inner.get("type") not in ("message", "app_mention"):
        return None
    if inner.get("subtype") == "bot_message" or inner.get("bot_id"):
        return None  # our own echo or another bot: never route, never loop
    if inner.get("subtype") in ("message_changed", "message_deleted", "channel_join"):
        return None
    text = (inner.get("text") or "").strip()
    if not text:
        return None
    return {
        "event_id": event.get("envelope_id", ""),
        "type": inner["type"],
        "text": text,
        "user_id": inner.get("user", ""),
        "channel_id": inner.get("channel", ""),
        "thread_ts": inner.get("thread_ts") or inner.get("ts", ""),
        "ts": inner.get("ts", ""),
    }


async def run_listener(
    *,
    app_token: str,
    bot_token: str,
    on_event: EventHandler,
) -> None:
    """Hold the socket forever. Import of slack_sdk happens HERE, not at module import, so
    unit tests and the API process never pay for a dependency only the listener needs."""
    require_tokens(app_token=app_token, bot_token=bot_token)
    try:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError as exc:
        raise SlackSocketNotConfigured(
            "pacote slack-sdk ausente: pip install slack-sdk"
        ) from exc

    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    web = AsyncWebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)

    async def _handle(_client: Any, req: SocketModeRequest) -> None:
        # Acknowledge FIRST, process after: Slack retries unacked envelopes, and a retry is a
        # duplicate task without idempotency. Ack is cheap; the handler is idempotent anyway
        # via event_id -> idempotency_key.
        await _client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        if req.type != "events_api":
            return
        normalised = normalise_event({"type": "events_api", "envelope_id": req.envelope_id,
                                      "event": req.payload.get("event", {})})
        if normalised is not None:
            await on_event(normalised)

    client.socket_mode_request_listeners.append(_handle)
    await client.connect()
    # Park forever; supervisor (systemd/Coolify restart policy) owns the lifecycle.
    import asyncio

    await asyncio.Event().wait()

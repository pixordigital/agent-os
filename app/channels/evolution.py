"""WhatsApp channel over Evolution API (self-hosted Baileys/Meta gateway).

Scope: config from env, one send primitive, webhook normalisation into intake shape.
It does NOT route or execute — handle_event owns the pipeline, same as Slack.

Owner: platform.

Why Evolution and not the Cloud API directly: the VPS already runs it, one instance
per number with QR pairing, and outbound is a plain POST — no template pre-approval
dance for agent replies. Cost truth stays honest: Meta per-message fees do NOT apply
on this path (own infra); Cloud API pricing only matters if a number migrates there.

Env: EVOLUTION_API_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE,
EVOLUTION_WEBHOOK_TOKEN (optional shared guard on our webhook route).
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class EvolutionNotConfigured(RuntimeError):
    """Raised when the webhook fires without server config. Loud, like Slack tokens:
    a WhatsApp intake that silently drops is worse than one that refuses."""


def settings() -> dict[str, str]:
    """Read env on every call, not at import: Coolify restarts pick up rotated keys
    without a rebuild, and tests never touch the process environment."""
    return {
        "base_url": (os.environ.get("EVOLUTION_API_URL") or "").rstrip("/"),
        "api_key": (os.environ.get("EVOLUTION_API_KEY") or "").strip(),
        "instance": (os.environ.get("EVOLUTION_INSTANCE") or "").strip(),
        "webhook_token": (os.environ.get("EVOLUTION_WEBHOOK_TOKEN") or "").strip(),
    }


def require_settings(cfg: dict[str, str]) -> dict[str, str]:
    if not cfg["base_url"] or not cfg["api_key"] or not cfg["instance"]:
        raise EvolutionNotConfigured(
            "EVOLUTION_API_URL, EVOLUTION_API_KEY e EVOLUTION_INSTANCE precisam estar "
            "configurados (Coolify > agent-os > Environment Variables)."
        )
    return cfg


async def send_text(
    *,
    to: str,
    text: str,
    client: httpx.AsyncClient | None = None,
    cfg: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST message/sendText to the configured instance. Returns the gateway envelope;
    the caller keeps message ids for threading, never here."""
    cfg = require_settings(cfg or settings())
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.post(
            f"{cfg['base_url']}/message/sendText/{cfg['instance']}",
            json={"number": to, "text": text},
            headers={"apikey": cfg["api_key"]},
        )
        response.raise_for_status()
        return response.json()
    finally:
        if own:
            await client.aclose()


def _message_text(message: dict[str, Any]) -> str:
    """Baileys shapes: plain conversation, extended text, or nothing worth routing."""
    if not isinstance(message, dict):
        return ""
    if isinstance(message.get("conversation"), str):
        return message["conversation"].strip()
    extended = message.get("extendedTextMessage") or {}
    if isinstance(extended, dict) and isinstance(extended.get("text"), str):
        return extended["text"].strip()
    return ""


def normalise_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Translate an Evolution messages.upsert webhook into intake shape. None for
    anything not worth routing: own echoes (fromMe would loop), status broadcasts,
    groups (support arrives per roadmap, not by accident), empty bodies. Pure,
    tested per branch — same loop discipline as the Slack intake."""
    if event.get("event") != "messages.upsert":
        return None
    data = event.get("data") or {}
    messages = data.get("messages") or []
    if not messages:
        return None
    first = messages[0] or {}
    key = first.get("key") or {}
    if key.get("fromMe") is True:
        return None
    remote = str(key.get("remoteJid") or "")
    if not remote or remote == "status@broadcast" or remote.endswith("@g.us"):
        return None
    text = _message_text(first.get("message") or {})
    if not text:
        return None
    number = remote.split("@")[0]
    return {
        "event_id": f"wa-{key.get('id', '')}",
        "type": "whatsapp_message",
        "text": text,
        "user_id": number,
        "channel_id": number,
        "channel_key": "",
        "mentioned_agent_key": "",
        "source": "whatsapp",
        "channel_ref": number,
        "push_name": first.get("pushName", ""),
        "ts": str(first.get("messageTimestamp") or ""),
    }

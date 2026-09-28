"""Slack Web API client: post, update, and HITL Block Kit cards over plain httpx.

Scope: outbound messages only. No SDK — the Web API surface we use (chat.postMessage,
chat.update) is three endpoints, and a dependency would buy nothing except opacity. Inbound
events arrive via Socket Mode (slack_socket.py), because this host has no public TLS ingress
and Socket Mode is outbound-only.

Owner: platform. Every function takes an explicit token; nothing reads env implicitly, so
tests inject fakes and production passes settings. No token, no call — functions raise
SlackNotConfigured instead of failing halfway through a workflow.
"""

from __future__ import annotations

from typing import Any

import httpx

SLACK_API = "https://slack.com/api"


class SlackNotConfigured(RuntimeError):
    """Raised when Slack is invoked without SLACK_BOT_TOKEN. Loud on purpose: a silent no-op
    here would make HITL approvals vanish — the human never sees the card, the task waits
    forever, and nobody knows why."""


class SlackError(RuntimeError):
    """Slack answered ok:false. Carries the error code (channel_not_found, not_in_channel,
    rate_limited...) so the caller can decide: retry, escalate, or give up loudly."""


def _require_token(token: str) -> str:
    if not (token or "").strip():
        raise SlackNotConfigured(
            "SLACK_BOT_TOKEN ausente. Crie o app em api.slack.com, instale no workspace "
            "e exporte o token xoxb-*. Sem ele, o worker registra o aviso e a task espera."
        )
    return token.strip()


def hitl_card(
    *,
    title: str,
    summary: str,
    context: str,
    approve_value: str,
    reject_value: str,
) -> list[dict[str, Any]]:
    """HITL approval card. Block Kit shapes per the slack-bot skill: header (plain_text, no
    mrkdwn), section with mrkdwn body, actions with Approve/Reject, context footer. The
    `value` fields carry "<approval_id>:<decision>" so the interactivity handler needs no
    lookup to know what was decided — the button IS the decision record."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": title[:150], "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary[:3000]},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Aprovar", "emoji": True},
                    "style": "primary",
                    "action_id": "hitl_approve",
                    "value": approve_value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Rejeitar", "emoji": True},
                    "style": "danger",
                    "action_id": "hitl_reject",
                    "value": reject_value,
                },
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": context[:2000]}],
        },
    ]


def status_blocks(*, title: str, lines: list[str], footer: str = "") -> list[dict[str, Any]]:
    """Plain status update: header + bullet section + optional context. No buttons, no
    interactivity — information only."""
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150], "emoji": True}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(f"• {line}" for line in lines)[:3000]},
        },
    ]
    if footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": footer[:2000]}]}
        )
    return blocks


async def post_message(
    *,
    token: str,
    channel: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST chat.postMessage. Returns the envelope (caller keeps `ts` for threading). Always
    sends `text` alongside blocks: it is the fallback for notifications and readers."""
    _require_token(token)
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks[:50]  # Slack hard limit; truncate, never 400.
    if thread_ts:
        payload["thread_ts"] = thread_ts
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await client.post(
            f"{SLACK_API}/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {token.strip()}"},
        )
    finally:
        if own:
            await client.aclose()
    return _checked(response.json())


async def update_message(
    *,
    token: str,
    channel: str,
    ts: str,
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """POST chat.update. Used for live progress (throttled by the caller, never here) and for
    freezing a HITL card after decision."""
    _require_token(token)
    payload: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks[:50]
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        response = await client.post(
            f"{SLACK_API}/chat.update",
            json=payload,
            headers={"Authorization": f"Bearer {token.strip()}"},
        )
    finally:
        if own:
            await client.aclose()
    return _checked(response.json())


def _checked(envelope: dict[str, Any]) -> dict[str, Any]:
    if not envelope.get("ok"):
        raise SlackError(envelope.get("error", "unknown_error"))
    return envelope

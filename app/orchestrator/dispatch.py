"""Deterministic dispatch: event -> team -> task skeleton.

Scope: pure routing. Given the org's teams/agents and a normalised intake event, decide which
team owns it, whether a specific agent is addressed, what task type it is, and what risk class
applies. Returns data; the caller (Slack intake, scheduler, API) writes the task row.

Owner: platform.

Routing rules, in order (first match wins, everything else is an explicit fallback):
  1. Explicit address: `@agent` mention or `/run <agent>` resolves to that agent's team.
  2. Team channel: a message in `#team-<key>` belongs to that team.
  3. Keyword match against team keys/names (deterministic substring, longest match wins).
  4. Fallback: the team's manager when exactly one manager exists, else unrouted (orchestrator
     holds it and escalates — routing to a random team would be worse than not routing).

Risk default is 'baixo'; anything that looks like money, external send, production or delete
escalates the SUGGESTED risk to 'alto' so policy review starts from the cautious side. The
policy engine (not this file) makes the final decision — this is triage, not judgement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HIGH_RISK_HINTS = (
    # Money and commitments. Deliberately PT + EN: the CEO writes PT, tool output is EN.
    "r$", "usd", "pagamento", "payment", "preço", "price", "desconto", "discount",
    "produção", "production", "producao", "deploy", "enviar", "send", "cliente",
    "customer", "contrato", "contract", "deletar", "delete", "apagar", "refund",
    "reembolso", "fatura", "invoice",
)


@dataclass(frozen=True)
class Route:
    """A routing decision. `agent_id=None` means "the team's manager triages" — the normal
    case, and the reason managers exist."""

    team_id: str | None
    agent_id: str | None
    task_type: str
    risk: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


def _slug(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def route_event(
    *,
    teams: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    text: str,
    channel_key: str = "",
    mentioned_agent_key: str = "",
    source: str = "",
    channel_ref: str = "",
) -> Route:
    """Route one normalised intake event. Pure: same inputs, same route, forever."""
    lowered = f" {text.lower()} "
    by_key = {_slug(t.get("key", "")): t for t in teams if t.get("key")}
    agents_by_key = {_slug(a.get("key", "")): a for a in agents if a.get("key")}
    managers = [a for a in agents if a.get("kind") == "manager" and a.get("status") == "active"]

    # 1. Explicit address wins over everything.
    if mentioned_agent_key:
        agent = agents_by_key.get(_slug(mentioned_agent_key))
        if agent is not None:
            return Route(
                team_id=agent.get("team_id"),
                agent_id=agent["id"],
                task_type="direct",
                risk=_suggest_risk(lowered),
                reason=f"menção direta a @{mentioned_agent_key}",
                payload=_base_payload(source, channel_ref, text),
            )
        return Route(
            team_id=None, agent_id=None, task_type="direct", risk="medio",
            reason=f"menção a @{mentioned_agent_key}, que não existe",
            payload=_base_payload(source, channel_ref, text),
        )

    # 2. Team channel owns its messages.
    if channel_key:
        team = by_key.get(_slug(channel_key).removeprefix("team-"))
        if team is not None:
            return Route(
                team_id=team["id"], agent_id=None, task_type="team_inbox",
                risk=_suggest_risk(lowered),
                reason=f"mensagem no canal do time {team['key']}",
                payload=_base_payload(source, channel_ref, text),
            )

    # 3. Keyword match: longest team key/name wins (dev-comercial beats dev).
    best: dict[str, Any] | None = None
    best_len = 0
    for team in teams:
        for candidate in (team.get("key", ""), team.get("name", "")):
            cand = candidate.strip().lower()
            if len(cand) >= 3 and f" {cand} " in f" {lowered} " and len(cand) > best_len:
                best, best_len = team, len(cand)
    if best is not None:
        manager = next(
            (m for m in managers if m.get("team_id") == best["id"]), None
        )
        return Route(
            team_id=best["id"],
            agent_id=manager["id"] if manager else None,
            task_type="keyword",
            risk=_suggest_risk(lowered),
            reason=f"menção a '{best['key']}' no texto",
            payload=_base_payload(source, channel_ref, text),
        )

    # 4. Fallback: single active manager, else explicitly unrouted.
    if len(managers) == 1:
        return Route(
            team_id=managers[0].get("team_id"), agent_id=managers[0]["id"],
            task_type="fallback", risk=_suggest_risk(lowered),
            reason="sem match: único manager ativo recebe",
            payload=_base_payload(source, channel_ref, text),
        )
    return Route(
        team_id=None, agent_id=None, task_type="unrouted",
        risk="medio", reason="sem match e sem manager único: orchestrator segura",
        payload=_base_payload(source, channel_ref, text),
    )


def _suggest_risk(lowered_text: str) -> str:
    if any(hint in lowered_text for hint in HIGH_RISK_HINTS):
        return "alto"
    return "baixo"


def _base_payload(source: str, channel_ref: str, text: str) -> dict[str, Any]:
    return {"source": source, "channel_ref": channel_ref, "text": text}

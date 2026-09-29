"""Owner command parser: PT-BR text -> management operations.

Scope: recognise the small, closed set of owner commands ("criar agente X no time Y",
"pausar agente X", "meta 50000 comercial", "status") and turn them into structured ops.
Anything else is NOT a command — it flows to dispatch as agent work. Pure functions,
tested per branch: a parser bug either ignores the owner or, worse, deletes the
wrong agent.

Matching is substring + name lookup against live teams/agents (passed in, never
queried here), so "pausar o backend" finds key/name containing "backend". Ambiguous
or missing targets resolve to `need_clarification`, never to a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Op:
    """A parsed owner command. `action` is the verb; `params` carries resolved ids."""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    reply: str = ""


def _find_agent(agents: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    lowered = text.lower()
    best: dict[str, Any] | None = None
    best_len = 0
    for agent in agents:
        for candidate in (agent.get("key", ""), agent.get("name", "")):
            cand = candidate.strip().lower()
            if len(cand) >= 3 and cand in lowered and len(cand) > best_len:
                best, best_len = agent, len(cand)
    return best


def _find_team(teams: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    lowered = text.lower()
    best: dict[str, Any] | None = None
    best_len = 0
    for team in teams:
        for candidate in (team.get("key", ""), team.get("name", "")):
            cand = candidate.strip().lower()
            if len(cand) >= 3 and cand in lowered and len(cand) > best_len:
                best, best_len = team, len(cand)
    return best


def _money_number(text: str) -> float | None:
    """First money-like number in owner text: 50.000, 50000, R$ 12,5."""
    match = re.search(r"(\d[\d.]*(?:,\d{1,2})?)", text.replace("R$", ""))
    if not match:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


def parse_command(
    *,
    text: str,
    teams: list[dict[str, Any]],
    agents: list[dict[str, Any]],
) -> Op | None:
    """Parse owner management intent. None means "not a command, route as work"."""
    lowered = text.lower().strip()

    if re.search(r"\b(ajuda|help|comandos)\b", lowered):
        return Op(
            action="help",
            reply=("Comandos: `criar agente <nome> [no time <time>]`, `criar time <nome>`, "
                   "`pausar|ativar|excluir <agente>`, `status`, `meta <valor> [time]`"),
        )

    if re.search(r"\bstatus\b", lowered):
        return Op(action="status", reply="")

    create_agent = re.search(r"criar agente\s+(.+)", lowered)
    if create_agent:
        rest = create_agent.group(1)
        team = _find_team(teams, rest)
        name = re.sub(r"\bno time\b.*", "", rest).strip(" .")
        if not name:
            return Op(action="need_clarification",
                      reply="Qual o nome do agente? Ex.: `criar agente sdr no time comercial`")
        return Op(action="create_agent",
                  params={"name": name.title(), "team_id": team["id"] if team else None},
                  reply="")

    create_team = re.search(r"criar time\s+(.+)", lowered)
    if create_team:
        name = create_team.group(1).strip(" .")
        if not name:
            return Op(action="need_clarification", reply="Qual o nome do time?")
        return Op(action="create_team", params={"name": name.title()}, reply="")

    for verb, action in (("pausar", "pause"), ("ativar", "resume"),
                         ("excluir", "delete"), ("deletar", "delete"), ("apagar", "delete")):
        if re.search(rf"\b{verb}\b", lowered):
            agent = _find_agent(agents, lowered)
            if agent is None:
                return Op(action="need_clarification",
                          reply=f"Qual agente {verb}? Não encontrei esse nome.")
            return Op(action=action, params={"agent_id": agent["id"],
                                             "name": agent.get("name", "")}, reply="")

    meta = re.search(r"\bmeta\b", lowered)
    if meta:
        value = _money_number(lowered)
        if value is None:
            return Op(action="need_clarification",
                      reply="Qual o valor da meta? Ex.: `meta 50000 comercial`")
        team = _find_team(teams, lowered)
        return Op(action="set_goal",
                  params={"target_brl": value,
                           "team_id": team["id"] if team else None,
                           "team_name": team.get("name") if team else None}, reply="")

    return None

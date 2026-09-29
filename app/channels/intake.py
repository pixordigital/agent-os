"""Intake handler: normalised event -> owner op or routed task -> reply text.

Scope: the pipeline both chat channels share. Commands execute on the management
plane immediately (no worker needed to create an agent); anything else routes via
dispatch, lands as a queued task row, and — when a worker pool is passed — enqueues
for execution. Channel specifics (Slack post, WhatsApp send) stay outside: the
caller passes `reply` and `enqueue` callables, or nothing.

Owner: platform.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from app import agents as agents_svc
from app.agents import AgentError
from app.channels.commands import parse_command
from app.orchestrator.dispatch import route_event

log = logging.getLogger("agentos.intake")

ReplyFn = Callable[[str], Awaitable[None]]
EnqueueFn = Callable[[str], Awaitable[None]]


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) < 2:
        raise AgentError("Não consegui derivar um identificador desse nome.")
    return slug


async def execute_op(op: Any, *, org_id: str) -> str:
    """Run one parsed owner command, return the reply text. AgentError → caller
    turns it into the reply, so a failed op answers instead of vanishing."""
    params = op.params
    if op.action == "help":
        return op.reply
    if op.action == "need_clarification":
        return op.reply
    if op.action == "status":
        agents = await agents_svc.list_agents(org_id)
        teams = await agents_svc.list_teams(org_id)
        active = sum(1 for a in agents if a.get("status") == "active")
        return f"{len(agents)} agente(s), {active} ativo(s), {len(teams)} time(s)."
    if op.action == "create_team":
        team = await agents_svc.create_team(
            org_id, {"name": params["name"], "key": _slug(params["name"])})
        return f"Time {team['name']} criado."
    if op.action == "create_agent":
        model = (params.get("model") or "").strip()
        if not model:
            return ("Qual modelo? Ex.: `criar agente sdr openrouter/anthropic/claude-sonnet-4` — "
                    "todo agente precisa de um modelo.")
        agent = await agents_svc.create_agent(org_id, {
            "name": params["name"], "key": _slug(params["name"]),
            "team_id": params.get("team_id"), "model": model,
            "status": "draft",
        })
        return f"Agente {agent['name']} criado como rascunho. Ative quando revisar o prompt."
    if op.action in ("pause", "resume"):
        agent = await agents_svc.set_agent_status(
            org_id, params["agent_id"],
            "paused" if op.action == "pause" else "active")
        state = "pausado" if op.action == "pause" else "ativo"
        return f"Agente {agent['name']} {state}."
    if op.action == "delete":
        agent = await agents_svc.delete_agent(org_id, params["agent_id"])
        return f"Agente {agent['name']} excluído. Histórico de custo permanece."
    if op.action == "set_goal":
        month = agents_svc.current_month()
        goal = await agents_svc.set_goal(
            org_id, params.get("team_id"), month, params["target_brl"])
        where = f" do time {params['team_name']}" if params.get("team_name") else " da empresa"
        target = f"{float(goal['target_brl']):.2f}".replace(".", ",")
        return f"Meta de {month}{where}: R$ {target}."
    return "Não entendi — `ajuda` lista os comandos."


async def handle_event(
    event: dict[str, Any],
    *,
    org_id: str,
    teams: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    reply: ReplyFn | None = None,
    enqueue: EnqueueFn | None = None,
) -> str | None:
    """One normalised event in, reply text out (also sent via `reply` when given).
    Never raises: intake that throws is intake that silently drops, so AgentError
    becomes the reply and anything else is logged with the event id."""
    text = str(event.get("text") or "")
    event_id = str(event.get("event_id") or "")
    try:
        op = parse_command(text=text, teams=teams, agents=agents)
        if op is not None:
            answer = await execute_op(op, org_id=org_id)
        else:
            route = route_event(
                teams=teams, agents=agents, text=text,
                channel_key=str(event.get("channel_key") or ""),
                mentioned_agent_key=str(event.get("mentioned_agent_key") or ""),
                source=str(event.get("source") or ""),
                channel_ref=str(event.get("channel_ref") or ""),
            )
            if route.team_id is None:
                answer = f"Recebi, mas não roteei: {route.reason}."
            else:
                task = await agents_svc.create_task(
                    org_id, team_id=route.team_id, agent_id=route.agent_id,
                    type=f"inbox-{route.task_type}", risk=route.risk,
                    payload={"text": text, "source": event.get("source"),
                             "channel_ref": event.get("channel_ref")},
                    idempotency_key=event_id or f"manual-{hash(text)}",
                )
                if enqueue is not None:
                    try:
                        await enqueue(task["id"])
                    except Exception as exc:  # noqa: BLE001 - queued row survives
                        log.warning("enqueue failed, task %s waits: %s", task["id"], exc)
                        answer = (f"Tarefa {task['id'][:8]} registrada ({route.reason}); "
                                  "worker offline, executa quando voltar.")
                    else:
                        answer = (f"Tarefa {task['id'][:8]} na fila ({route.reason}).")
                else:
                    answer = (f"Tarefa {task['id'][:8]} registrada ({route.reason}); "
                              "sem worker ligado, executa quando voltar.")
    except AgentError as exc:
        answer = str(exc)
    except Exception as exc:  # noqa: BLE001 - intake never throws
        log.warning("intake failed on event %s: %s", event_id, exc)
        answer = "Falhei ao processar. Tenta de novo em outras palavras?"
    if reply is not None:
        try:
            await reply(answer)
        except Exception as exc:  # noqa: BLE001 - answer still returned
            log.warning("reply failed: %s", exc)
    return answer

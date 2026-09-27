"""Agent management: CRUD for agents, teams and departments, plus monitoring reads.

Scope: the operator console's data layer. Every call goes through the service-key PostgREST
client (BYPASSRLS), because this UI *is* the owner view — it shows platform truth, not a
tenant's view. Tenant-scoped reads arrive with Supabase Auth in Wave 2, never here.

Owner: platform. What it does NOT do: execute agents, route tasks, or touch Slack. Those are
Wave 2+ and live in their own modules; this file must stay boring CRUD plus aggregation.

Validation here is for good error messages only. The database CHECKs are the enforcer — an app
that forgets a rule must not be able to save a broken agent.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from app.db import db

# Single-tenant bootstrap for Wave 1. Multi-org onboarding is Wave 8; until then exactly one
# org exists and it is created here, idempotently, instead of pretending the UI supports orgs
# it cannot create.
DEFAULT_ORG_SLUG = "default"
DEFAULT_ORG_NAME = "Minha empresa"

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
BACKENDS = ("native", "claude_code", "codex", "opencode", "antigravity")
KINDS = ("orchestrator", "manager", "worker", "reviewer")
STATUSES = ("draft", "active", "paused", "disabled")

UPDATABLE_AGENT_FIELDS = {
    "name", "team_id", "department_id", "parent_agent_id", "kind", "backend",
    "model", "fallback_model", "system_prompt", "tools", "status",
    "budget_brl_day", "max_tokens_per_task", "max_tool_calls",
}


class AgentError(ValueError):
    """Domain error with a message already in PT-BR, ready for the UI."""


def _check_key(key: str) -> str:
    key = (key or "").strip().lower()
    if not KEY_RE.match(key):
        raise AgentError(
            "Identificador inválido: use minúsculas, números e hífens "
            "(ex.: dev-backend, crm-qualificador)."
        )
    return key


def _check_enum(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise AgentError(f"{label} inválido: escolha entre {', '.join(allowed)}.")
    return value


def _check_agent_payload(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    """Validate and normalise an agent payload. partial=True means PATCH (subset allowed)."""
    out: dict[str, Any] = {}
    if not partial or "name" in payload:
        name = (payload.get("name") or "").strip()
        if len(name) < 2:
            raise AgentError("O agente precisa de um nome (mínimo 2 letras).")
        out["name"] = name
    if not partial or "key" in payload:
        if "key" in payload or not partial:
            out["key"] = _check_key(payload.get("key", ""))
    for field in ("kind", "backend", "status"):
        if field in payload and payload[field] not in (None, ""):
            allowed = {"kind": KINDS, "backend": BACKENDS, "status": STATUSES}[field]
            out[field] = _check_enum(str(payload[field]), allowed, field)
    if not partial or "model" in payload:
        model = (payload.get("model") or "").strip()
        if not model:
            raise AgentError(
                "Todo agente precisa de um modelo — o produto inteiro é escolha de modelo "
                "por agente. Ex.: openrouter/anthropic/claude-sonnet-4."
            )
        out["model"] = model
    for field in ("fallback_model", "system_prompt"):
        if field in payload:
            out[field] = (payload[field] or "").strip()
    if "tools" in payload:
        tools = payload["tools"]
        if not isinstance(tools, list):
            raise AgentError("Ferramentas precisam ser uma lista.")
        out["tools"] = tools
    for fk in ("team_id", "department_id", "parent_agent_id"):
        if fk in payload:
            out[fk] = payload[fk] or None
    for num in ("budget_brl_day", "max_tokens_per_task", "max_tool_calls"):
        if num in payload and payload[num] not in (None, ""):
            try:
                out[num] = float(payload[num]) if num == "budget_brl_day" else int(payload[num])
            except (TypeError, ValueError):
                raise AgentError(f"{num} precisa ser um número.") from None
            if out[num] <= 0:
                raise AgentError(f"{num} precisa ser maior que zero.")
    return out


async def ensure_default_org() -> dict[str, Any]:
    """Return the Wave-1 org, creating it on first boot.

    Idempotent by slug. Called from the app lifespan, so the UI never renders "no org" and no
    human step is needed between deploy and first agent. Wave 8 replaces this with onboarding.
    """
    existing = await db.svc.get(
        "/organizations", params={"slug": f"eq.{DEFAULT_ORG_SLUG}", "limit": "1"}
    )
    existing.raise_for_status()
    rows = existing.json()
    if rows:
        return rows[0]
    created = await db.svc.post(
        "/organizations",
        json={"slug": DEFAULT_ORG_SLUG, "name": DEFAULT_ORG_NAME, "settings": {}},
        headers={"Prefer": "return=representation"},
    )
    created.raise_for_status()
    return created.json()[0]


# ------------------------------------------------------------------ agents


async def list_agents(org_id: str, team_id: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"org_id": f"eq.{org_id}", "order": "name", "limit": "200"}
    if team_id:
        params["team_id"] = f"eq.{team_id}"
    response = await db.svc.get("/agents", params=params)
    response.raise_for_status()
    return response.json()


async def get_agent(org_id: str, agent_id: str) -> dict[str, Any]:
    response = await db.svc.get(
        "/agents",
        params={"org_id": f"eq.{org_id}", "id": f"eq.{agent_id}", "limit": "1"},
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise AgentError("Agente não encontrado.")
    return rows[0]


async def create_agent(org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = _check_agent_payload(payload, partial=False)
    data["org_id"] = org_id
    response = await db.svc.post(
        "/agents", json=data, headers={"Prefer": "return=representation"}
    )
    if response.status_code == 409:
        raise AgentError(
            f"Já existe um agente com o identificador '{data['key']}' nesta empresa."
        )
    response.raise_for_status()
    return response.json()[0]


async def update_agent(org_id: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = _check_agent_payload(payload, partial=True)
    unknown = set(payload) - UPDATABLE_AGENT_FIELDS - {"key"}
    if unknown:
        raise AgentError(f"Campos desconhecidos: {', '.join(sorted(unknown))}.")
    if not data:
        raise AgentError("Nada para atualizar.")
    data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    response = await db.svc.patch(
        "/agents",
        params={"org_id": f"eq.{org_id}", "id": f"eq.{agent_id}"},
        json=data,
        headers={"Prefer": "return=representation"},
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise AgentError("Agente não encontrado.")
    return rows[0]


async def set_agent_status(org_id: str, agent_id: str, status: str) -> dict[str, Any]:
    """Pause/resume/disable. Only status moves here — never runtime_state, which belongs to
    worker heartbeats (Wave 2). The UI must not be able to fake "running"."""
    _check_enum(status, STATUSES, "status")
    response = await db.svc.patch(
        "/agents",
        params={"org_id": f"eq.{org_id}", "id": f"eq.{agent_id}"},
        json={
            "status": status,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        headers={"Prefer": "return=representation"},
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        raise AgentError("Agente não encontrado.")
    return rows[0]


async def agent_runs(
    org_id: str, agent_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    response = await db.svc.get(
        "/agent_runs",
        params={
            "org_id": f"eq.{org_id}",
            "agent_id": f"eq.{agent_id}",
            "order": "created_at.desc",
            "limit": str(min(max(limit, 1), 200)),
        },
    )
    response.raise_for_status()
    return response.json()


async def agent_tasks(
    org_id: str, agent_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    response = await db.svc.get(
        "/tasks",
        params={
            "org_id": f"eq.{org_id}",
            "assigned_agent_id": f"eq.{agent_id}",
            "order": "updated_at.desc",
            "limit": str(min(max(limit, 1), 100)),
        },
    )
    response.raise_for_status()
    return response.json()


async def agent_stats(org_id: str, agent_id: str) -> dict[str, Any]:
    """24h aggregates from recorded runs. Everything here is measured; with no workers running
    yet (Wave 2) every number is honestly zero — and the UI says so instead of hiding it."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    response = await db.svc.get(
        "/agent_runs",
        params={
            "org_id": f"eq.{org_id}",
            "agent_id": f"eq.{agent_id}",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": "500",
        },
    )
    response.raise_for_status()
    runs = response.json()
    total = len(runs)
    succeeded = sum(1 for r in runs if r.get("success") is True)
    cost = sum(float(r.get("cost_brl") or 0) for r in runs)
    latencies = [r["latency_ms"] for r in runs if r.get("latency_ms")]
    cached = sum(r.get("cached_tokens") or 0 for r in runs)
    created = sum((r.get("tokens_in") or 0) + (r.get("cache_creation_tokens") or 0) for r in runs)
    return {
        "runs_24h": total,
        "success_rate": (succeeded / total) if total else None,
        "cost_brl_24h": round(cost, 4),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        # Measured cache hit rate, never an assumed saving. None with no data, not 0%.
        "cache_hit_rate": (cached / (cached + created)) if (cached + created) else None,
    }


# ------------------------------------------------------------------ teams & departments


async def list_teams(org_id: str) -> list[dict[str, Any]]:
    response = await db.svc.get(
        "/teams", params={"org_id": f"eq.{org_id}", "order": "name", "limit": "100"}
    )
    response.raise_for_status()
    return response.json()


async def create_team(org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = _check_key(payload.get("key", ""))
    name = (payload.get("name") or "").strip()
    if len(name) < 2:
        raise AgentError("O time precisa de um nome (mínimo 2 letras).")
    data: dict[str, Any] = {"org_id": org_id, "key": key, "name": name}
    if payload.get("department_id"):
        data["department_id"] = payload["department_id"]
    response = await db.svc.post(
        "/teams", json=data, headers={"Prefer": "return=representation"}
    )
    if response.status_code == 409:
        raise AgentError(f"Já existe um time com o identificador '{key}'.")
    response.raise_for_status()
    return response.json()[0]


async def list_departments(org_id: str) -> list[dict[str, Any]]:
    response = await db.svc.get(
        "/departments", params={"org_id": f"eq.{org_id}", "order": "name", "limit": "100"}
    )
    response.raise_for_status()
    return response.json()


async def create_department(org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = _check_key(payload.get("key", ""))
    name = (payload.get("name") or "").strip()
    if len(name) < 2:
        raise AgentError("O departamento precisa de um nome (mínimo 2 letras).")
    data: dict[str, Any] = {
        "org_id": org_id,
        "key": key,
        "name": name,
        "mission": (payload.get("mission") or "").strip(),
    }
    if payload.get("team_id"):
        data["team_id"] = payload["team_id"]
    response = await db.svc.post(
        "/departments", json=data, headers={"Prefer": "return=representation"}
    )
    if response.status_code == 409:
        raise AgentError(f"Já existe um departamento com o identificador '{key}'.")
    response.raise_for_status()
    return response.json()[0]


async def dashboard_summary(org_id: str) -> dict[str, Any]:
    """Everything the home page needs in three round-trips. Aggregation happens in Python
    because at Wave-1 scale (tens of agents, hundreds of runs) a materialised view would be a
    second source of truth to keep fresh for no measurable gain."""
    agents = await list_agents(org_id)
    teams = await list_teams(org_id)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat()
    runs_resp = await db.svc.get(
        "/agent_runs",
        params={
            "org_id": f"eq.{org_id}",
            "created_at": f"gte.{since}",
            "order": "created_at.desc",
            "limit": "1000",
        },
    )
    runs_resp.raise_for_status()
    runs = runs_resp.json()

    by_agent: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_agent.setdefault(run.get("agent_id") or "", []).append(run)

    team_ids = {t["id"]: t for t in teams}
    for agent in agents:
        agent_runs = by_agent.get(agent["id"], [])
        agent["runs_24h"] = len(agent_runs)
        agent["cost_24h"] = round(sum(float(r.get("cost_brl") or 0) for r in agent_runs), 4)
        agent["team_name"] = (team_ids.get(agent.get("team_id") or "") or {}).get("name")

    by_status: dict[str, int] = {}
    for agent in agents:
        by_status[agent["status"]] = by_status.get(agent["status"], 0) + 1

    return {
        "agents": agents,
        "teams": teams,
        "agent_count": len(agents),
        "by_status": by_status,
        "runs_24h": len(runs),
        "cost_24h": round(sum(float(r.get("cost_brl") or 0) for r in runs), 4),
    }

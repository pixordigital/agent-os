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
    "budget_brl_day", "max_tokens_per_task", "max_tool_calls", "temperature",
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
    if "temperature" in payload and payload["temperature"] not in (None, ""):
        try:
            out["temperature"] = float(payload["temperature"])
        except (TypeError, ValueError):
            raise AgentError("Temperatura precisa ser um número entre 0 e 2.") from None
        if not 0 <= out["temperature"] <= 2:
            raise AgentError("Temperatura precisa estar entre 0 (determinístico) e 2.")
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
    agent = response.json()[0]
    await emit_event(
        org_id, agent["id"], "created",
        {"key": agent["key"], "name": agent["name"], "backend": agent.get("backend"),
         "model": agent.get("model")},
        team_id=agent.get("team_id"),
    )
    return agent


async def update_agent(org_id: str, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = _check_agent_payload(payload, partial=True)
    unknown = set(payload) - UPDATABLE_AGENT_FIELDS - {"key"}
    if unknown:
        raise AgentError(f"Campos desconhecidos: {', '.join(sorted(unknown))}.")
    if not data:
        raise AgentError("Nada para atualizar.")
    changed = sorted(k for k in data if k != "updated_at")
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
    agent = rows[0]
    await emit_event(
        org_id, agent["id"], "updated", {"fields": changed},
        team_id=agent.get("team_id"),
    )
    return agent


async def set_agent_status(org_id: str, agent_id: str, status: str) -> dict[str, Any]:
    """Pause/resume/disable. Only status moves here — never runtime_state, which belongs to
    worker heartbeats (Wave 2). The UI must not be able to fake "running"."""
    _check_enum(status, STATUSES, "status")
    before = await get_agent(org_id, agent_id)
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
    agent = rows[0]
    await emit_event(
        org_id, agent["id"], "status_changed",
        {"from": before.get("status"), "to": status},
        team_id=agent.get("team_id"),
    )
    return agent


async def delete_agent(org_id: str, agent_id: str) -> dict[str, Any]:
    """Delete an agent. Children detach (parent FK is SET NULL), run history stays —
    cost truth must survive the agent. The timeline row carries agent_id None because
    the agent's own events cascade away with it."""
    agent = await get_agent(org_id, agent_id)
    response = await db.svc.delete(
        "/agents", params={"org_id": f"eq.{org_id}", "id": f"eq.{agent_id}"}
    )
    response.raise_for_status()
    await emit_event(
        org_id, None, "deleted",
        {"key": agent["key"], "name": agent["name"]},
        team_id=agent.get("team_id"),
    )
    return agent


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


TERMINAL_TASK_STATUSES = ("completed", "failed", "expired", "cancelled")

KIND_ORDER = {"orchestrator": 0, "manager": 1, "worker": 2, "reviewer": 3}


async def emit_event(
    org_id: str,
    agent_id: str | None,
    kind: str,
    payload: dict[str, Any] | None = None,
    team_id: str | None = None,
) -> None:
    """Append one row to the operational timeline. Best effort on purpose: this is the
    operational feed, not the probative trail (that is audit_logs). An admin action that
    succeeded must not fail because its timeline row did not."""
    import logging

    try:
        await db.svc.post(
            "/agent_events",
            json={
                "org_id": org_id,
                "agent_id": agent_id,
                "team_id": team_id,
                "kind": kind,
                "payload": payload or {},
            },
        )
    except Exception as exc:  # noqa: BLE001 - timeline loss is logged, never fatal
        logging.getLogger("agentos").warning("agent_events append failed: %s", exc)


def build_tree(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nest agents into a forest by parent_agent_id. Cycle-safe: nodes unreachable from any
    root (the signature of a cycle, or a parent deleted out of band) are surfaced as detached
    roots instead of vanishing — a node the chart cannot show is a node nobody manages."""
    by_id: dict[str, dict[str, Any]] = {
        a["id"]: {**a, "children": [], "detached": False} for a in agents
    }
    roots: list[dict[str, Any]] = []
    for agent in agents:
        node = by_id[agent["id"]]
        pid = agent.get("parent_agent_id")
        if pid and pid in by_id and pid != agent["id"]:
            by_id[pid]["children"].append(node)
        else:
            roots.append(node)

    reachable: set[str] = set()

    def mark(node: dict[str, Any]) -> None:
        if node["id"] in reachable:
            return
        reachable.add(node["id"])
        for child in node["children"]:
            mark(child)

    for root in roots:
        mark(root)
    for agent in agents:
        if agent["id"] not in reachable:
            node = by_id[agent["id"]]
            node["detached"] = True
            roots.append(node)

    def sort_key(node: dict[str, Any]) -> tuple[int, str]:
        return (KIND_ORDER.get(node.get("kind", ""), 9), node.get("name", ""))

    def sort_all(node: dict[str, Any]) -> None:
        node["children"].sort(key=sort_key)
        for child in node["children"]:
            sort_all(child)

    roots.sort(key=sort_key)
    for root in roots:
        sort_all(root)
    return roots


async def team_detail(org_id: str, team_id: str) -> dict[str, Any]:
    """Everything the team page needs. Aggregation in Python at Wave-1 scale; a materialised
    view becomes justified when a team has hundreds of agents, not before."""
    teams = await list_teams(org_id)
    team = next((t for t in teams if t["id"] == team_id), None)
    if team is None:
        raise AgentError("Time não encontrado.")

    members = await list_agents(org_id, team_id)
    member_ids = {m["id"] for m in members}
    now = dt.datetime.now(dt.timezone.utc)
    week_ago = (now - dt.timedelta(days=7)).isoformat()

    runs_resp = await db.svc.get(
        "/agent_runs",
        params={
            "org_id": f"eq.{org_id}",
            "created_at": f"gte.{week_ago}",
            "order": "created_at.desc",
            "limit": "2000",
        },
    )
    runs_resp.raise_for_status()
    team_runs = [r for r in runs_resp.json() if r.get("agent_id") in member_ids]
    day_ago = (now - dt.timedelta(hours=24)).isoformat()
    runs_24h = [r for r in team_runs if (r.get("created_at") or "") >= day_ago]

    def _cost(rs: list[dict[str, Any]]) -> float:
        return round(sum(float(r.get("cost_brl") or 0) for r in rs), 4)

    def _success(rs: list[dict[str, Any]]) -> float | None:
        return (sum(1 for r in rs if r.get("success") is True) / len(rs)) if rs else None

    by_agent: dict[str, list[dict[str, Any]]] = {}
    for run in team_runs:
        by_agent.setdefault(run.get("agent_id") or "", []).append(run)
    for member in members:
        member_runs = by_agent.get(member["id"], [])
        member["runs_7d"] = len(member_runs)
        member["cost_7d"] = _cost(member_runs)
        member["runs_24h"] = sum(1 for r in member_runs if (r.get("created_at") or "") >= day_ago)
        member["cost_24h"] = _cost(
            [r for r in member_runs if (r.get("created_at") or "") >= day_ago]
        )

    tasks_resp = await db.svc.get(
        "/tasks",
        params={
            "org_id": f"eq.{org_id}",
            "team_id": f"eq.{team_id}",
            "order": "updated_at.desc",
            "limit": "500",
        },
    )
    tasks_resp.raise_for_status()
    tasks = tasks_resp.json()
    open_tasks = [t for t in tasks if t.get("status") not in TERMINAL_TASK_STATUSES]
    by_status: dict[str, int] = {}
    for task in tasks:
        by_status[task.get("status", "?")] = by_status.get(task.get("status", "?"), 0) + 1
    sla_breaches = [
        t for t in open_tasks
        if t.get("sla_deadline") and t["sla_deadline"] < now.isoformat()
    ]
    oldest_open = min(
        (t.get("created_at") or "" for t in open_tasks), default=None
    )

    models: dict[str, dict[str, Any]] = {}
    for run in team_runs:
        entry = models.setdefault(
            run.get("model") or "?", {"runs": 0, "cost": 0.0}
        )
        entry["runs"] += 1
        entry["cost"] = round(entry["cost"] + float(run.get("cost_brl") or 0), 4)

    autonomy: dict[str, int] = {}
    for member in members:
        autonomy[member["status"]] = autonomy.get(member["status"], 0) + 1

    return {
        "team": team,
        "members": members,
        "runs_24h": len(runs_24h),
        "runs_7d": len(team_runs),
        "success_24h": _success(runs_24h),
        "cost_24h": _cost(runs_24h),
        "cost_7d": _cost(team_runs),
        "tasks_total": len(tasks),
        "tasks_open": len(open_tasks),
        "tasks_by_status": by_status,
        "sla_breaches": sla_breaches,
        "oldest_open_at": oldest_open,
        "models": models,
        "autonomy": autonomy,
        # No detectors yet: degradation detection is Wave 4. An empty list here means
        # "not watched", and the template says exactly that instead of showing zero.
        "alerts": [],
    }


async def recent_events(
    org_id: str, agent_id: str | None = None, team_id: str | None = None, limit: int = 30
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "org_id": f"eq.{org_id}",
        "order": "created_at.desc",
        "limit": str(min(max(limit, 1), 100)),
    }
    if agent_id:
        params["agent_id"] = f"eq.{agent_id}"
    if team_id:
        params["team_id"] = f"eq.{team_id}"
    response = await db.svc.get("/agent_events", params=params)
    response.raise_for_status()
    return response.json()


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

"""AgentOS API — the single ingress, port 7777.

Scope: process lifecycle, health/readiness, the operator console (pages + JSON), and the
agent/team/department management API. It does NOT execute agents — that is Wave 2+ and lives
in the worker and the backends. Owner: platform.

Why the port is fixed at 7777: the Coolify domain, the health check, the runbook and the
monitoring all point at it. Moving it is a coordinated change for no benefit.

Entry points in the same image (see Dockerfile):
  api        — this app
  worker     — ARQ, leases tasks and executes them (Wave 2)
  scheduler  — the one and only cron; POSTs to edge functions (Wave 2)

This console is the OWNER view: reads and writes go through the service key (BYPASSRLS) and
show platform truth, never a tenant's view. Tenant-scoped surfaces arrive with Supabase Auth
in Wave 2.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import agents as agents_svc
from app.agents import AgentError
from app.config import get_settings
from app.db import db
from app.prompt_templates import get_template, list_templates

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agentos")

WAVE1_TABLES = ("tasks", "policies", "people", "agent_runs")


async def _org_id() -> str:
    """Wave-1 single tenant. Wave 8 replaces this with the session's org."""
    org = await agents_svc.ensure_default_org()
    return str(org["id"])


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 - FastAPI signature
    """Connect, migrate, ensure the Wave-1 org, serve.

    A migration failure is fatal on purpose: booting against a half-applied schema and
    answering 200 is how a missing ALTER becomes a 3am incident. If `migrate()` throws, the
    container crashloops and Coolify surfaces it.
    """
    settings = get_settings()
    log.info("agentos starting env=%s port=%s", settings.app_env, settings.port)
    await db.connect()
    applied, pending = await db.migrate()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    if pending:
        log.warning("pending migrations: %s", ", ".join(pending))
    # PostgREST caches the schema at boot: a fresh table is invisible to it until reload.
    # Without this, creating `agents` in 002 would 404 on reads until the rest container
    # restarted — a silent lag between "migration applied" and "API works".
    try:
        async with db.pool.acquire() as conn:
            await conn.execute("SELECT pg_notify('pgrst', 'reload schema')")
    except Exception as exc:  # noqa: BLE001 - best effort; a stale cache heals on restart
        log.warning("postgrest schema reload notify failed: %s", exc)
    org = await agents_svc.ensure_default_org()
    log.info("org ready: %s (%s)", org["name"], org["slug"])
    log.info("agentos ready")
    try:
        yield
    finally:
        await db.disconnect()


app = FastAPI(
    title="AgentOS",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
)

templates = Jinja2Templates(directory="app/templates")


def _template(name: str, request: Request, **context: Any) -> HTMLResponse:
    context.setdefault("tab", "")
    return templates.TemplateResponse(request=request, name=name, context=context)


# ------------------------------------------------------------------ health


@app.get("/live", tags=["health"])
async def live() -> dict[str, str]:
    """Liveness: the process is up. Deliberately checks nothing.

    A liveness probe that touches the database gets the container killed during a database
    blip, which turns a recoverable dependency outage into a restart loop.
    """
    return {"status": "live"}


@app.get("/ready", tags=["health"])
async def ready(response: Response) -> dict[str, Any]:
    """Readiness: Postgres reachable, migrations applied, RLS actually enforced.

    503 when degraded, so Coolify stops routing traffic to a container that would lie.
    """
    health = await db.health()
    if not health.ready:
        response.status_code = 503
    return health.as_dict()


@app.get("/healthz", tags=["health"])
async def healthz() -> dict[str, Any]:
    """Aggregate JSON for monitoring, without the 503 semantics of /ready."""
    return (await db.health()).as_dict()


@app.get("/api/status", tags=["api"])
async def api_status() -> dict[str, Any]:
    """Counts + health + agent/team summary for the dashboard."""
    org_id = await _org_id()
    health = await db.health()
    counts: dict[str, int | None] = {}
    for table in WAVE1_TABLES:
        counts[table] = await db.count(table)
    return {
        "health": health.as_dict(),
        "counts": counts,
        "env": get_settings().app_env,
        "summary": await agents_svc.dashboard_summary(org_id),
    }


# ------------------------------------------------------------------ pages


@app.get("/", response_class=Response)
async def index(request: Request) -> Response:
    return _template(
        "dashboard.html", request, tab="home", port=get_settings().port
    )


@app.get("/agents", response_class=Response)
async def agents_page(
    request: Request, error: str = "", team_id: str = ""
) -> Response:
    org_id = await _org_id()
    agents = await agents_svc.list_agents(org_id)
    teams = await agents_svc.list_teams(org_id)
    summary = await agents_svc.dashboard_summary(org_id)
    for agent in agents:
        match = next((a for a in summary["agents"] if a["id"] == agent["id"]), {})
        agent["runs_24h"] = match.get("runs_24h", 0)
        agent["cost_24h"] = match.get("cost_24h", 0)
        agent["team_name"] = match.get("team_name")
    return _template(
        "agents.html",
        request,
        tab="agents",
        agents=agents,
        teams=teams,
        kinds=agents_svc.KINDS,
        backends=agents_svc.BACKENDS,
        error=error,
        form={"team_id": team_id},
    )


@app.get("/agents/table", response_class=Response)
async def agents_table(request: Request) -> Response:
    """HTMX fragment: the monitor table body, polled every 15s. Server-rendered so the numbers
    on screen went through the same code path as the full page — no second implementation to
    drift."""
    org_id = await _org_id()
    summary = await agents_svc.dashboard_summary(org_id)
    return _template("agents_table.html", request, agents=summary["agents"])


@app.get("/org", response_class=Response)
async def org_page(request: Request) -> Response:
    """Live org chart: orchestrator -> managers -> agents, with observed state per node.

    "Live" today means HTMX polling (fragment below), because the trimmed stack has no
    Realtime container. The poller stays as the fallback even after Realtime lands in Wave 2.
    """
    org_id = await _org_id()
    summary = await agents_svc.dashboard_summary(org_id)
    for agent in summary["agents"]:
        agent["team_name"] = agent.get("team_name")
    tree = agents_svc.build_tree(summary["agents"])
    events = await agents_svc.recent_events(org_id, limit=20)
    return _template(
        "org.html", request, tab="org", tree=tree, events=events,
        agent_count=summary["agent_count"],
    )


@app.get("/org/tree", response_class=Response)
async def org_tree(request: Request) -> Response:
    """HTMX fragment for the chart, polled every 10s. Same builder as the full page."""
    org_id = await _org_id()
    summary = await agents_svc.dashboard_summary(org_id)
    return _template("org_tree.html", request, tree=agents_svc.build_tree(summary["agents"]))


@app.post("/agents", response_class=Response)
async def agents_create(
    request: Request,
    name: str = Form(default=""),
    key: str = Form(default=""),
    team_id: str = Form(default=""),
    kind: str = Form(default="worker"),
    backend: str = Form(default="native"),
    model: str = Form(default=""),
    fallback_model: str = Form(default=""),
    temperature: str = Form(default="0.7"),
    max_tokens: str = Form(default=""),
    max_tool_calls: str = Form(default=""),
    budget_brl_day: str = Form(default=""),
    system_prompt: str = Form(default=""),
    tools: str = Form(default=""),
    active_now: str = Form(default=""),
) -> Response:
    org_id = await _org_id()
    form = {
        "name": name, "key": key, "team_id": team_id, "kind": kind,
        "backend": backend, "model": model, "fallback_model": fallback_model,
        "temperature": temperature, "max_tokens_per_task": max_tokens,
        "max_tool_calls": max_tool_calls, "budget_brl_day": budget_brl_day,
        "system_prompt": system_prompt,
        "tools": [t.strip() for t in tools.split(",") if t.strip()],
    }
    try:
        agent = await agents_svc.create_agent(
            org_id, {**form, "status": "active" if active_now else "draft"}
        )
    except AgentError as exc:
        teams = await agents_svc.list_teams(org_id)
        agents = await agents_svc.list_agents(org_id)
        return _template(
            "agents.html", request, tab="agents", agents=agents, teams=teams,
            kinds=agents_svc.KINDS, backends=agents_svc.BACKENDS,
            error=str(exc), form=form,
        )
    return RedirectResponse(f"/agents/{agent['id']}", status_code=303)


@app.get("/agents/{agent_id}", response_class=Response)
async def agent_detail_page(
    request: Request, agent_id: str, error: str = ""
) -> Response:
    org_id = await _org_id()
    try:
        agent = await agents_svc.get_agent(org_id, agent_id)
    except AgentError:
        return RedirectResponse("/agents", status_code=303)
    teams = await agents_svc.list_teams(org_id)
    stats = await agents_svc.agent_stats(org_id, agent_id)
    runs = await agents_svc.agent_runs(org_id, agent_id)
    tasks = await agents_svc.agent_tasks(org_id, agent_id)
    all_agents = await agents_svc.list_agents(org_id)
    return _template(
        "agent_detail.html", request, tab="agents", agent=agent, teams=teams,
        agents=all_agents, stats=stats, runs=runs, tasks=tasks,
        backends=agents_svc.BACKENDS, kinds=agents_svc.KINDS, error=error, form={},
    )


@app.post("/agents/{agent_id}", response_class=Response)
async def agent_edit(
    request: Request,
    agent_id: str,
    name: str = Form(default=""),
    team_id: str = Form(default=""),
    kind: str = Form(default=""),
    parent_agent_id: str = Form(default=""),
    status: str = Form(default=""),
    backend: str = Form(default="native"),
    model: str = Form(default=""),
    fallback_model: str = Form(default=""),
    temperature: str = Form(default=""),
    max_tokens: str = Form(default=""),
    max_tool_calls: str = Form(default=""),
    budget_brl_day: str = Form(default=""),
    system_prompt: str = Form(default=""),
    tools: str = Form(default=""),
) -> Response:
    org_id = await _org_id()
    form = {
        "name": name, "team_id": team_id, "kind": kind or None,
        "parent_agent_id": parent_agent_id or None,
        "status": status or None,
        "backend": backend, "model": model,
        "fallback_model": fallback_model, "temperature": temperature,
        "max_tokens_per_task": max_tokens, "max_tool_calls": max_tool_calls,
        "budget_brl_day": budget_brl_day, "system_prompt": system_prompt,
        "tools": [t.strip() for t in tools.split(",") if t.strip()],
    }
    form = {k: v for k, v in form.items() if v is not None}
    try:
        await agents_svc.update_agent(org_id, agent_id, form)
    except AgentError as exc:
        try:
            agent = await agents_svc.get_agent(org_id, agent_id)
        except AgentError:
            return RedirectResponse("/agents", status_code=303)
        teams = await agents_svc.list_teams(org_id)
        stats = await agents_svc.agent_stats(org_id, agent_id)
        runs = await agents_svc.agent_runs(org_id, agent_id)
        tasks = await agents_svc.agent_tasks(org_id, agent_id)
        all_agents = await agents_svc.list_agents(org_id)
        return _template(
            "agent_detail.html", request, tab="agents", agent=agent, teams=teams,
            agents=all_agents, stats=stats, runs=runs, tasks=tasks,
            backends=agents_svc.BACKENDS, kinds=agents_svc.KINDS, error=str(exc), form=form,
        )
    return RedirectResponse(f"/agents/{agent_id}", status_code=303)


@app.post("/agents/{agent_id}/pause", response_class=Response)
async def agent_pause(request: Request, agent_id: str) -> Response:
    org_id = await _org_id()
    try:
        await agents_svc.set_agent_status(org_id, agent_id, "paused")
    except AgentError:
        pass
    referer = request.headers.get("referer", "")
    target = f"/agents/{agent_id}" if f"/agents/{agent_id}" in referer else "/agents"
    return RedirectResponse(target, status_code=303)


@app.post("/agents/{agent_id}/resume", response_class=Response)
async def agent_resume(request: Request, agent_id: str) -> Response:
    org_id = await _org_id()
    try:
        await agents_svc.set_agent_status(org_id, agent_id, "active")
    except AgentError:
        pass
    referer = request.headers.get("referer", "")
    target = f"/agents/{agent_id}" if f"/agents/{agent_id}" in referer else "/agents"
    return RedirectResponse(target, status_code=303)


@app.get("/teams", response_class=Response)
async def teams_page(
    request: Request, team_error: str = "", dept_error: str = ""
) -> Response:
    org_id = await _org_id()
    summary = await agents_svc.dashboard_summary(org_id)
    departments = await agents_svc.list_departments(org_id)
    by_team: dict[str, list[dict[str, Any]]] = {}
    for agent in summary["agents"]:
        by_team.setdefault(agent.get("team_id") or "", []).append(agent)
    return _template(
        "teams.html", request, tab="teams", teams=summary["teams"],
        agents_by_team=by_team,
        unassigned=by_team.get("", []),
        departments=departments,
        team_error=team_error, dept_error=dept_error,
    )


@app.get("/teams/{team_id}", response_class=Response)
async def team_detail_page(request: Request, team_id: str) -> Response:
    """Team dashboard: members, 24h/7d runs and cost, autonomy mix, models in use, backlog by
    status, SLA breaches. Every number measured; empty sections say what is missing instead of
    showing zero as if it meant something."""
    org_id = await _org_id()
    try:
        detail = await agents_svc.team_detail(org_id, team_id)
    except AgentError:
        return RedirectResponse("/teams", status_code=303)
    events = await agents_svc.recent_events(org_id, team_id=team_id, limit=15)
    return _template(
        "team_detail.html", request, tab="teams", detail=detail, events=events
    )


@app.post("/teams", response_class=Response)
async def teams_create(
    request: Request, name: str = Form(default=""), key: str = Form(default="")
) -> Response:
    org_id = await _org_id()
    try:
        await agents_svc.create_team(org_id, {"name": name, "key": key})
    except AgentError as exc:
        return await teams_page(request, team_error=str(exc))
    return RedirectResponse("/teams", status_code=303)


@app.post("/departments", response_class=Response)
async def departments_create(
    request: Request,
    name: str = Form(default=""),
    key: str = Form(default=""),
    mission: str = Form(default=""),
) -> Response:
    org_id = await _org_id()
    try:
        await agents_svc.create_department(
            org_id, {"name": name, "key": key, "mission": mission}
        )
    except AgentError as exc:
        return await teams_page(request, dept_error=str(exc))
    return RedirectResponse("/teams", status_code=303)


# ------------------------------------------------------------------ JSON API (workers, Slack, future frontends)


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


@app.get("/api/agents")
async def api_agents_list(team_id: str = "") -> JSONResponse:
    org_id = await _org_id()
    return JSONResponse(
        {"ok": True, "agents": await agents_svc.list_agents(org_id, team_id or None)}
    )


@app.post("/api/agents")
async def api_agents_create(payload: dict[str, Any]) -> JSONResponse:
    org_id = await _org_id()
    try:
        agent = await agents_svc.create_agent(org_id, payload)
    except AgentError as exc:
        return _json_error(str(exc))
    return JSONResponse({"ok": True, "agent": agent}, status_code=201)


@app.get("/api/agents/{agent_id}")
async def api_agent_detail(agent_id: str) -> JSONResponse:
    org_id = await _org_id()
    try:
        agent = await agents_svc.get_agent(org_id, agent_id)
    except AgentError as exc:
        return _json_error(str(exc), 404)
    return JSONResponse(
        {
            "ok": True,
            "agent": agent,
            "stats": await agents_svc.agent_stats(org_id, agent_id),
            "runs": await agents_svc.agent_runs(org_id, agent_id),
            "tasks": await agents_svc.agent_tasks(org_id, agent_id),
        }
    )


@app.patch("/api/agents/{agent_id}")
async def api_agent_update(agent_id: str, payload: dict[str, Any]) -> JSONResponse:
    org_id = await _org_id()
    try:
        agent = await agents_svc.update_agent(org_id, agent_id, payload)
    except AgentError as exc:
        return _json_error(str(exc))
    return JSONResponse({"ok": True, "agent": agent})


@app.get("/api/prompt-templates")
async def api_prompt_templates() -> JSONResponse:
    """Template gallery metadata (no content — the form fetches one at a time)."""
    return JSONResponse({"ok": True, **list_templates()})


@app.get("/api/prompt-templates/{key}")
async def api_prompt_template(key: str) -> JSONResponse:
    template = get_template(key)
    if template is None:
        return _json_error("Modelo de prompt não encontrado.", 404)
    return JSONResponse({"ok": True, "template": template})

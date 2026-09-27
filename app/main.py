"""AgentOS API — the single ingress, port 7777.

Scope: process lifecycle, health/readiness, and the operator dashboard shell. It does NOT do
business orchestration — that lives in modules and behind PostgREST. Owner: platform.

Why the port is fixed at 7777: the Coolify domain, the health check, the runbook and the
monitoring all point at it. Moving it is a coordinated change for no benefit.

Entry points in the same image (see Dockerfile):
  api        — this app
  worker     — ARQ, leases tasks and executes them
  scheduler  — the one and only cron; POSTs to edge functions
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import db

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("agentos")

# The counts the dashboard shows. Each one goes through PostgREST/RLS, so a number on screen is
# never broader than what the viewer is allowed to see.
WAVE1_TABLES = ("tasks", "policies", "people", "agent_runs")


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 - FastAPI signature
    """Connect, migrate, serve. A migration failure is fatal on purpose.

    Booting against a half-applied schema and answering 200 is how a missing ALTER becomes a
    3am incident. If `migrate()` throws, the container crashloops and Coolify surfaces it.
    """
    settings = get_settings()
    log.info("agentos starting env=%s port=%s", settings.app_env, settings.port)
    await db.connect()
    applied, pending = await db.migrate()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    if pending:
        log.warning("pending migrations: %s", ", ".join(pending))
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
    """Counts + health for the dashboard. Counts are RLS-scoped reads via PostgREST."""
    health = await db.health()
    counts: dict[str, int | None] = {}
    for table in WAVE1_TABLES:
        counts[table] = await db.count(table)
    return {
        "health": health.as_dict(),
        "counts": counts,
        "env": get_settings().app_env,
    }


@app.get("/", response_class=Response)
async def index(request: Request) -> Response:
    """Operator dashboard shell (Jinja2 + HTMX, no build step).

    Renders static structure on purpose: the live org chart arrives in Wave 2, and a dashboard
    that draws empty placeholder nodes is worse than one that does not draw them yet.
    """
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"port": get_settings().port, "tables": WAVE1_TABLES},
    )

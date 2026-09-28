"""execute_task: claim -> policy -> budget -> LLM -> record -> complete.

Scope: one task, start to finish, for the `native` backend. Every step is a database
transition through the state machine — never an in-memory flag — so a crashed worker leaves
a task the next worker can pick up, not a ghost.

Owner: platform.

Failure matrix (retry only what a retry can fix):
  transport/timeout/5xx/429 -> retry with backoff (ARQ Retry), task back to queued
  4xx/auth/bad envelope      -> failed, no retry (retrying a bad request burns money)
  policy DENY                -> failed with reason, no LLM call, no token spent
  no policy / REQUIRE_HITL   -> waiting_human + pending_approvals row, no LLM call
  budget exceeded            -> waiting_human + pending_approvals (budget), no LLM call
  attempts exhausted         -> failed (dead-letter by status; DLQ table is Wave 4)

Stop conditions (LLM10 Unbounded Consumption, enforced BEFORE the call):
  agent.max_tokens_per_task, task.budget_brl via agent daily budget pre-check.
  There is no post-hoc refund for tokens, so the check that matters runs first.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from arq import Retry

from app.db import Database
from app.guardrails.input import normalise
from app.llm import openrouter
from app.llm.prompt_prefix import build_block0, build_block1

log = logging.getLogger("agentos.worker")

LEASE_SECONDS = 300

wdb = Database()


async def _set_org(conn: Any, org_id: str) -> None:
    """Scope every statement in this transaction to the task's org. FORCE RLS applies to the
    worker too — without this, the worker sees zero rows (fail-closed) instead of leaking
    across tenants. str() because asyncpg returns UUID objects and set_config wants text."""
    await conn.execute("SELECT set_config('app.current_org_id', $1, true)", str(org_id))


async def _decide_policy(conn: Any, org_id: str, action: str) -> dict[str, Any] | None:
    """Highest-precedence active policy for the action. None means "no policy" — which is
    REQUIRE_HITL by the fail-closed rule, never ALLOW."""
    row = await conn.fetchrow(
        """
        SELECT decision, required_role, approval_timeout, fallback_action
          FROM policies
         WHERE org_id = $1 AND action = $2 AND status = 'active'
         ORDER BY priority ASC, version DESC
         LIMIT 1
        """,
        org_id, action,
    )
    return dict(row) if row else None


async def _daily_cost(conn: Any, org_id: str, agent_id: str) -> float:
    row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(cost_brl), 0) AS total FROM agent_runs
         WHERE org_id = $1 AND agent_id = $2::uuid
           AND created_at >= date_trunc('day', now())
        """,
        org_id, agent_id,
    )
    return float(row["total"])


async def _record_run(
    conn: Any, *, org_id: str, team_id: str | None, agent_id: str, task_id: str,
    agent: dict[str, Any], result: openrouter.LlmResult, success: bool,
) -> None:
    await conn.execute(
        """
        INSERT INTO agent_runs
          (org_id, team_id, agent_id, task_id, backend, model, success,
           tokens_in, tokens_out, cached_tokens, cache_creation_tokens,
           cost_usd, cost_brl, provider_request_id, latency_ms)
        VALUES ($1,$2,$3::uuid,$4::uuid,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        """,
        org_id, team_id, agent_id, task_id,
        agent.get("backend") or "native", result.model or agent.get("model") or "",
        success, result.tokens_in, result.tokens_out, result.cached_tokens,
        result.cache_creation_tokens, result.cost_usd, result.cost_brl,
        result.provider_request_id, result.latency_ms,
    )


async def _set_status(
    conn: Any, task_id: str, version: int, status: str, **extra: Any
) -> None:
    """Move a task through the state machine. The trigger rejects illegal transitions and
    stale versions — this function does not reimplement either check."""
    sets = ", ".join(f"{k} = ${i + 4}" for i, k in enumerate(extra))
    sql = (f"UPDATE tasks SET status = $2, version = $3{', ' + sets if sets else ''} "
           f"WHERE id = $1::uuid")
    await conn.execute(sql, task_id, status, version + 1, *extra.values())


async def execute_task(ctx: dict[str, Any], task_id: str) -> dict[str, Any]:
    """ARQ job. `ctx` carries the worker pool; everything else comes from the database, so a
    retried job re-reads fresh state instead of trusting a stale argument."""
    import os

    pool = ctx["pool"] if "pool" in ctx else wdb.pool
    async with pool.acquire() as conn:
        task = await conn.fetchrow("SELECT * FROM tasks WHERE id = $1::uuid", task_id)
        if task is None:
            return {"ok": False, "error": "task_not_found"}
        task = dict(task)
        await _set_org(conn, task["org_id"])

        # Claim: only a queued task with a free or expired lease moves. SKIP LOCKED so N
        # workers never block on each other; the version bump makes double-claims impossible.
        # The walk queued -> assigned -> leased -> running happens inside ONE transaction holding
        # the row lock: three separate UPDATEs because the state machine (rightly) has no
        # queued -> running shortcut, and weakening the machine to fit the worker would be
        # backwards.
        lease_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=LEASE_SECONDS)
        async with conn.transaction():
            claimed = await conn.fetchrow(
                """
                SELECT * FROM tasks WHERE id = $1::uuid AND status = 'queued'
                   AND (lease_until IS NULL OR lease_until < now())
                FOR UPDATE SKIP LOCKED
                """,
                task_id,
            )
            if claimed is None:
                return {"ok": False, "error": "not_claimable"}
            await _set_status(conn, task_id, task["version"], "assigned")
            await _set_status(conn, task_id, task["version"] + 1, "leased",
                              lease_until=lease_until, lease_owner="worker")
            await _set_status(conn, task_id, task["version"] + 2, "running")
        task["version"] += 3
        task["status"] = "running"

        agent = await conn.fetchrow("SELECT * FROM agents WHERE id = $1::uuid",
                                    task.get("assigned_agent_id"))
        if agent is None:
            await _set_status(conn, task_id, task["version"], "failed",
                              error="no_agent_assigned")
            return {"ok": False, "error": "no_agent_assigned"}
        agent = dict(agent)

        if agent.get("status") != "active":
            await _set_status(conn, task_id, task["version"], "failed",
                              error=f"agent_{agent.get('status')}")
            return {"ok": False, "error": "agent_not_active"}

        # Policy gate: no LLM call happens before this point. A DENY or a missing policy
        # costs exactly zero tokens — the cheapest call is the one never made.
        policy = await _decide_policy(conn, task["org_id"], task.get("type") or "generic")
        decision = (policy or {}).get("decision", "REQUIRE_HITL")
        if decision == "DENY":
            await _set_status(conn, task_id, task["version"], "failed",
                              error="policy_deny")
            return {"ok": False, "error": "policy_deny"}
        if decision != "ALLOW":
            approval = await conn.fetchrow(
                """INSERT INTO pending_approvals (org_id, team_id, task_id, kind, title, payload)
                   VALUES ($1, $2, $3::uuid, 'hitl', $4, $5)
                   ON CONFLICT (task_id) WHERE status = 'pending' DO NOTHING
                   RETURNING id""",
                task["org_id"], task.get("team_id"), task_id,
                f"HITL: {task.get('type')}",
                {"reason": "policy_require_hitl", "policy": policy},
            )
            await _set_status(conn, task_id, task["version"], "waiting_human")
            return {"ok": True, "waiting_human": True,
                    "approval_id": str(approval["id"]) if approval else None}

        # Budget pre-check (LLM10): today's spend vs the agent's daily ceiling.
        budget = agent.get("budget_brl_day")
        if budget is not None:
            spent = await _daily_cost(conn, task["org_id"], agent["id"])
            if spent >= float(budget):
                await conn.execute(
                    """INSERT INTO pending_approvals (org_id, team_id, task_id, kind, title, payload)
                       VALUES ($1, $2, $3::uuid, 'hitl', $4, $5)
                       ON CONFLICT (task_id) WHERE status = 'pending' DO NOTHING""",
                    task["org_id"], task.get("team_id"), task_id,
                    "Orçamento diário atingido",
                    {"reason": "budget_exceeded", "spent_brl": spent,
                     "budget_brl": float(budget)},
                )
                await _set_status(conn, task_id, task["version"], "waiting_human")
                return {"ok": True, "waiting_human": True, "reason": "budget_exceeded"}

        # Build the request: BLOCK 0 (immutable agent definition, cacheable) + BLOCK 1
        # (daily plan, cacheable) + volatile user message (external text, NEVER cached).
        payload = task.get("payload") or {}
        user_text = str(payload.get("text") or "")
        clean = normalise(user_text, source=str(payload.get("source") or "api"),
                          channel_ref=str(payload.get("channel_ref") or ""))
        try:
            system = build_block0(
                role_key=agent.get("key") or agent["id"],
                mission=(agent.get("system_prompt") or "").strip() or "Execute a tarefa.",
                sop="",
                policies=[],
                tools=[{"name": t} for t in (agent.get("tools") or [])],
                few_shots=[],
                output_schema={"type": "object"},
            )
        except Exception as exc:  # noqa: BLE001 - volatile content guard (prompt_prefix)
            await _set_status(conn, task_id, task["version"], "failed",
                              error=f"prefix_not_cacheable: {exc}")
            return {"ok": False, "error": "prefix_not_cacheable"}

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": clean.text},
        ]
        raw_temperature = agent.get("temperature")
        temperature = float(raw_temperature) if raw_temperature is not None else 0.7
        raw_max_tokens = agent.get("max_tokens_per_task")
        max_tokens = int(raw_max_tokens) if raw_max_tokens is not None else None
        result = await openrouter.chat(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            model=agent.get("model") or "",
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        await _record_run(
            conn, org_id=task["org_id"], team_id=task.get("team_id"),
            agent_id=agent["id"], task_id=task_id, agent=agent,
            result=result, success=result.ok and bool(result.text),
        )

        if not result.ok:
            if result.error in ("rate_limited",) or result.error.startswith(
                ("transport:", "provider_5xx:")
            ):
                if task["attempt"] + 1 >= task["max_attempts"]:
                    await _set_status(conn, task_id, task["version"], "failed",
                                      error=result.error, attempt=task["attempt"] + 1)
                    return {"ok": False, "error": "attempts_exhausted"}
                await _set_status(conn, task_id, task["version"], "retrying",
                                  attempt=task["attempt"] + 1, error=result.error)
                raise Retry(defer=30 * (task["attempt"] + 1))
            await _set_status(conn, task_id, task["version"], "failed", error=result.error)
            return {"ok": False, "error": result.error}

        await conn.execute(
            "UPDATE tasks SET status = 'completed', version = $2, result = $3 "
            "WHERE id = $1::uuid",
            task_id, task["version"] + 1,
            {"answer": result.text, "model": result.model,
             "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
             "cost_brl": result.cost_brl},
        )
        await conn.execute(
            """INSERT INTO agent_events (org_id, team_id, agent_id, kind, payload)
               VALUES ($1, $2, $3::uuid, 'task_completed', $4)""",
            task["org_id"], task.get("team_id"), agent["id"],
            {"task_id": task_id, "model": result.model},
        )
        return {"ok": True, "task_id": task_id}

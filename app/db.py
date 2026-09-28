"""Data access: Postgres pool, boot-time migrations, PostgREST client.

Scope: connection lifecycle and the readiness/health probes. It does NOT expose business
queries — those live in module repos. Owner: platform.

Two endpoints, on purpose:
  * ``database_url`` (direct Postgres) — DDL, migrations, server-side writes that need
    transactional control (lease claim, state transitions).
  * ``supabase_url`` (PostgREST through Kong) — tenant-scoped reads, where RLS is the only
    thing standing between one team and another team's data.

Going through Postgres for reads would bypass the anon-key/RLS path and make tenant isolation
a code convention instead of a database guarantee.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


async def _init_connection(conn: Any) -> None:
    """Per-connection setup: decode jsonb as dicts. asyncpg returns jsonb as raw text by
    default, which turns every `row["payload"]["text"]` into a TypeError at runtime — found
    live when the worker read its first task. One place, every pool, no per-query parsing."""
    import json

    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads,
        schema="pg_catalog", format="text",
    )


MIGRATIONS_TABLE = """
CREATE SCHEMA IF NOT EXISTS app_meta;
CREATE TABLE IF NOT EXISTS app_meta.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass
class Health:
    """Readiness detail. Every field is measured, never assumed."""

    postgres_ok: bool = False
    postgrest_ok: bool = False
    migrations_applied: list[str] = field(default_factory=list)
    migrations_pending: list[str] = field(default_factory=list)
    rls_enabled_on: list[str] = field(default_factory=list)
    detail: str = ""

    @property
    def ready(self) -> bool:
        """Ready means: Postgres reachable, every migration applied, RLS actually enabled.

        `pending migrations` blocks readiness on purpose. A container that boots against a stale
        schema and answers 200 is how you get a 3am incident that is really a missing ALTER.
        """
        return self.postgres_ok and not self.migrations_pending and bool(self.rls_enabled_on)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "postgres_ok": self.postgres_ok,
            "postgrest_ok": self.postgrest_ok,
            "migrations_applied": self.migrations_applied,
            "migrations_pending": self.migrations_pending,
            "rls_enabled_on": self.rls_enabled_on,
            "detail": self.detail,
        }


class Database:
    """Owns the asyncpg pool and the PostgREST client for the process lifetime."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None
        self._http: httpx.AsyncClient | None = None
        self._svc: httpx.AsyncClient | None = None

    async def connect(self) -> None:
        settings = get_settings()
        self._pool = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=5,  # ponytail: one api container, low concurrency. Raise with replicas.
            init=_init_connection,
        )
        # Both headers, on purpose. `apikey` is the Kong-layer convention; `Authorization:
        # Bearer` is what PostgREST itself requires to actually switch to the JWT role — verified
        # on the wire against postgrest v14.6, where `apikey` alone leaves the request running as
        # `authenticator` and every table denies it. Sending both keeps Kong (Wave 2) and direct
        # PostgREST working with zero branching.
        self._http = httpx.AsyncClient(
            base_url=settings.supabase_url,
            timeout=5.0,
            headers={
                "apikey": settings.supabase_anon_key,
                "Authorization": f"Bearer {settings.supabase_anon_key}",
            },
        )
        self._svc = httpx.AsyncClient(
            base_url=settings.supabase_url,
            timeout=5.0,
            headers={
                "apikey": settings.supabase_service_key,
                "Authorization": f"Bearer {settings.supabase_service_key}",
                # Ask for the real total: without it an empty table answers `Content-Range: */*`,
                # and `int("*")` is a ValueError that the blanket except below would swallow into
                # a None — this exact line cost an hour of debugging against a live stack.
                "Prefer": "count=exact",
            },
        )

    async def disconnect(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._svc is not None:
            await self._svc.aclose()
            self._svc = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._pool

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._http

    @property
    def svc(self) -> httpx.AsyncClient:
        """Owner-view client. Service key, BYPASSRLS, never exposed to browsers.

        Used only by the operator probe. It answers "how much data exists", never "what may
        this tenant see" — those are different questions and this client only answers the first.
        """
        if self._svc is None:
            raise RuntimeError("Database.connect() was not awaited")
        return self._svc

    # ---------------------------------------------------------------- migrations

    async def migrate(self) -> tuple[list[str], list[str]]:
        """Apply pending migrations in filename order. Returns (applied, pending).

        Migrations must be idempotent (``IF NOT EXISTS``) because this runs on every boot and
        Coolify restarts containers freely. Track versions in ``app_meta.schema_migrations``.

        DDL is not wrapped in a single transaction on purpose: on self-hosted Supabase the
        `postgres` role owns these objects, and a partial failure must leave the rest applied
        rather than roll back the extension installs.
        """
        settings = get_settings()
        directory = pathlib.Path(settings.migrations_dir)
        if not directory.is_dir():
            return [], []

        files = sorted(directory.glob("*.sql"))
        versions = [f.name for f in files]
        if not versions:
            return [], []

        async with self.pool.acquire() as conn:
            await conn.execute(MIGRATIONS_TABLE)
            rows = await conn.fetch("SELECT version FROM app_meta.schema_migrations")
            done = {r["version"] for r in rows}

        pending = [v for v in versions if v not in done]
        applied: list[str] = []
        for version in pending:
            sql = (directory / version).read_text(encoding="utf-8")
            log.info("applying migration %s", version)
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO app_meta.schema_migrations (version) VALUES ($1) "
                        "ON CONFLICT DO NOTHING",
                        version,
                    )
            applied.append(version)
        return applied, [v for v in versions if v not in done and v not in applied]

    # ---------------------------------------------------------------- probes

    async def health(self) -> Health:
        """Measure every readiness dependency. Never raises, never guesses."""
        health = Health()
        try:
            async with self.pool.acquire() as conn:
                health.postgres_ok = await conn.fetchval("SELECT 1") == 1
                health.migrations_applied = [
                    r["version"]
                    for r in await conn.fetch(
                        "SELECT version FROM app_meta.schema_migrations ORDER BY version"
                    )
                ]
                health.rls_enabled_on = [
                    r["tablename"]
                    for r in await conn.fetch(
                        """
                        SELECT c.relname AS tablename
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relkind = 'r'
                          AND c.relrowsecurity      -- RLS enabled
                          AND c.relforcerowsecurity -- ...and FORCED, not merely enabled
                        ORDER BY c.relname
                        """
                    )
                ]
        except Exception as exc:  # noqa: BLE001 - probe must never crash the app
            health.detail = f"postgres: {exc}"
            return health

        settings = get_settings()
        directory = pathlib.Path(settings.migrations_dir)
        if directory.is_dir():
            all_versions = {f.name for f in directory.glob("*.sql")}
            health.migrations_pending = sorted(all_versions - set(health.migrations_applied))

        try:
            prefix = get_settings().supabase_path_prefix.rstrip("/") or "/"
            response = await self.http.get(prefix)
            health.postgrest_ok = response.status_code < 500
            if not health.postgrest_ok:
                health.detail = f"postgrest: HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            health.detail = f"postgrest: {exc}"

        if not health.detail:
            health.detail = "ok" if health.ready else "degraded"
        return health

    async def count(self, table: str) -> int | None:
        """Owner-view row count through PostgREST with the service key.

        This answers "how much data exists on the platform" for the operator dashboard — not
        "what may this tenant see". Tenant-scoped reads go through the user's own JWT (Wave 2),
        never through this client. A role=anon JWT would be *correctly* denied here by the RLS
        FORCE policy, which is why the probe does not use it.

        Returns None on failure instead of 0. A zero that means "could not reach the database"
        is the kind of number that ends up on a dashboard and gets believed.
        """
        try:
            prefix = get_settings().supabase_path_prefix.rstrip("/")
            response = await self.svc.get(
                f"{prefix}/{table}", params={"select": "id", "limit": "1"}
            )
            response.raise_for_status()
            content_range = response.headers.get("content-range", "")
            if "/" in content_range:
                total = content_range.split("/")[-1]
                # `*/*` is a valid range for an empty result; only digits are a count.
                if total.isdigit():
                    return int(total)
            return len(response.json())
        except Exception:  # noqa: BLE001
            return None


db = Database()

"""Runtime configuration.

Scope: process-level settings only (port, endpoints, secrets). It does NOT read business
config from the database — that lives in ``org_config`` and is fetched per request, because a
setting that never changes should not require a deploy.

Owner: platform. Boundaries: no business logic, no DB access.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings.

    The UI port is 7777 by design: it is the single ingress for the operator dashboard and must
    not move, because the Coolify domain, the health checks and the runbook all point at it.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    port: int = 7777

    # Direct Postgres (DDL + migrations + server-side writes).
    # Self-hosted Supabase: reachable as `db:5432` from inside the Coolify network.
    database_url: str = "postgresql://postgres:postgres@localhost:5432/postgres"

    # PostgREST base URL. With Kong in front (Wave 2) this is the gateway and paths carry
    # the `/rest/v1` prefix; talking to PostgREST directly (Wave 1 trimmed stack) the prefix
    # MUST be empty, because PostgREST serves tables at `/` and `/rest/v1/...` 404s.
    # Verified the loud way: the app answered ready:true with all counts None for an hour
    # because it was requesting a Kong path from a Kongless backend.
    supabase_url: str = "http://api-gw:8000"
    supabase_path_prefix: str = "/rest/v1"
    supabase_anon_key: str = ""
    # Service key: SERVER SECRET, never leaves the container, never reaches a browser. Used only
    # by the operator probe below, because this dashboard IS the owner view: it shows platform
    # truth, not a tenant's view. Tenant-scoped reads arrive with Supabase Auth in Wave 2, via
    # the user's own JWT — not through this key.
    supabase_service_key: str = ""

    # Directory of idempotent .sql migrations, applied in filename order on boot.
    migrations_dir: str = "supabase/migrations"

    slack_bot_token: str = ""
    openrouter_api_key: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor.

    Cached because env vars are immutable for the process lifetime; re-reading them per request
    would be noise. Tests should call ``get_settings.cache_clear()``.
    """
    return Settings()

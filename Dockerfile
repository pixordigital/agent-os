# One image, three entrypoints. The Coolify app runs `api`; `worker` and `scheduler` are the
# same image with a different command, so there is a single build to keep patched and a single
# place where a dependency bump can break something.
#
# Migrations are baked in and applied on boot by app/db.py. That removes the "someone forgot to
# run the migration" step that turns into a 3am incident, at the cost of requiring every
# migration to be idempotent — which is enforced by review, see AGENTS.md.

FROM python:3.12-slim AS base

# curl is only here for the container healthcheck; libpq is what asyncpg needs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app

# Dependency layer first: code changes do not invalidate the wheel cache.
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32" "jinja2>=3.1" \
      "python-multipart>=0.0.18" \
      "asyncpg>=0.30" "httpx>=0.27" "pydantic>=2.9" "pydantic-settings>=2.6" "arq>=0.26"

COPY app ./app
COPY supabase/migrations ./supabase/migrations
COPY docs ./docs

# Never run as root: an agent that can write to the filesystem should not be able to write to
# the host's system directories either.
RUN useradd --system --uid 10001 agentos && chown -R agentos:agentos /srv/app
USER agentos

ENV APP_ENV=production \
    PORT=7777 \
    MIGRATIONS_DIR=supabase/migrations \
    PYTHONUNBUFFERED=1

EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/live" || exit 1

# Default role is the API. Coolify overrides CMD per service for worker and scheduler.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7777", "--proxy-headers"]

"""Worker entrypoint: `python -m app.worker`.

Scope: start the ARQ worker with the Redis URL from env. Nothing else lives here — job logic
is in tasks.py, scheduling policy (one scheduler only) arrives with Wave 4.

Owner: platform.
"""

from __future__ import annotations

import os

from arq import run_worker
from arq.connections import RedisSettings

from app.worker.tasks import wdb
from app.worker.tasks import execute_task


async def startup(ctx: dict) -> None:
    await wdb.connect()
    ctx["pool"] = wdb.pool


async def shutdown(ctx: dict) -> None:
    await wdb.disconnect()


class WorkerSettings:
    functions = (execute_task,)
    cron_jobs = ()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379"))
    max_jobs = 10  # ponytail: fixed small pool; autoscale by queue depth is Wave 4
    job_timeout = 600
    keep_result = 3600
    max_tries = 5
    retry_jobs = True


if __name__ == "__main__":
    run_worker(WorkerSettings)

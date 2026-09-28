"""Worker: leases tasks and executes them. The API never executes.

Scope: ARQ jobs. The only job in Wave 2 is execute_task (native backend). CLI backends,
schedules and the manager loop arrive in later waves through this same door.
Owner: platform.
"""
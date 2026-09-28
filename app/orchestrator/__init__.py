"""Orchestrator: deterministic dispatch. The LLM classifies; policy decides.

Scope: turn an intake event into a routed task (team, agent-or-manager, type, risk). Pure
functions — no I/O, no model calls — so routing is testable, repeatable and auditable.
Owner: platform.

What it does NOT do: execute anything, call any model, or approve anything. Execution is the
worker; approval is policy + human. An orchestrator that executes is a god service.
"""
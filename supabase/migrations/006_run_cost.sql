-- 006_run_cost.sql — measured provider cost on every run.
--
-- OpenRouter returns the real request cost in USD credits inside `usage`. Storing it (instead
-- of estimating from tokens × a price table that rots) is what makes every cost number on the
-- dashboard measured. cost_brl keeps the existing BRL convention via a documented conversion
-- constant in code; cost_usd is the auditable source next to the provider's request id.

ALTER TABLE public.agent_runs
  ADD COLUMN IF NOT EXISTS cost_usd numeric(12,6);
ALTER TABLE public.agent_runs
  ADD COLUMN IF NOT EXISTS provider_request_id text;
COMMENT ON COLUMN public.agent_runs.cost_usd IS
  'Provider-reported request cost in USD (OpenRouter usage.cost). NULL means the provider did
   not report one — never estimate silently; cost_brl without cost_usd is a conversion of an
   assumption and the UI must say so.';
COMMENT ON COLUMN public.agent_runs.provider_request_id IS
  'Provider request id for support/debugging and billing reconciliation.';

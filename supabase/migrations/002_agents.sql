-- 002_agents.sql — agents: the executable nodes of the org tree.
--
-- Idempotent like 001 (app/db.py applies it on every boot). Same conventions: CHECK instead of
-- ENUM, explicit policy statements instead of relying on the 001 loop (which cannot reach a
-- table created later), explicit GRANTs to service_role (BYPPASSRLS skips policy, not grants).

CREATE TABLE IF NOT EXISTS public.agents (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id           uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  department_id     uuid REFERENCES public.departments(id) ON DELETE SET NULL,
  parent_agent_id   uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  key               text NOT NULL,
  name              text NOT NULL,
  kind              text NOT NULL DEFAULT 'worker'
                      CHECK (kind IN ('orchestrator','manager','worker','reviewer')),
  backend           text NOT NULL DEFAULT 'native'
                      CHECK (backend IN ('native','claude_code','codex','opencode','antigravity')),
  model             text NOT NULL DEFAULT '',
  fallback_model    text NOT NULL DEFAULT '',
  system_prompt     text NOT NULL DEFAULT '',
  tools             jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- status is the ADMIN state (what the owner wants); runtime_state is the OBSERVED state
  -- (what the worker last reported). Pausing sets status; only a worker heartbeat may set
  -- runtime_state. Confusing the two is how a dashboard shows "running" for a dead agent.
  status            text NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft','active','paused','disabled')),
  runtime_state     text NOT NULL DEFAULT 'idle'
                      CHECK (runtime_state IN ('idle','busy','blocked','offline')),
  current_task_id   uuid REFERENCES public.tasks(id) ON DELETE SET NULL,
  last_heartbeat_at timestamptz,
  budget_brl_day    numeric(12,2) CHECK (budget_brl_day IS NULL OR budget_brl_day >= 0),
  max_tokens_per_task integer CHECK (max_tokens_per_task IS NULL OR max_tokens_per_task > 0),
  max_tool_calls    integer CHECK (max_tool_calls IS NULL OR max_tool_calls > 0),
  version           integer NOT NULL DEFAULT 1,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, key),
  -- A row may not parent itself. Deeper cycles are an admin error the UI surfaces (Wave 2 adds
  -- a guard trigger if delegation traversal ever needs the guarantee in the database).
  CHECK (parent_agent_id IS DISTINCT FROM id)
);
CREATE INDEX IF NOT EXISTS agents_org_team_idx  ON public.agents (org_id, team_id);
CREATE INDEX IF NOT EXISTS agents_parent_idx    ON public.agents (parent_agent_id);
CREATE INDEX IF NOT EXISTS agents_org_status_idx ON public.agents (org_id, status);
COMMENT ON TABLE public.agents IS
  'One row per agent: orchestrator -> managers -> workers/reviewers via parent_agent_id.
   model/fallback_model is the per-agent model choice (OpenRouter for native/opencode/codex,
   Anthropic-family for claude_code, Gemini for antigravity — see AGENTS.md for the verified
   limits of each). Per-task model overrides split out into agent_models in Wave 5; until then
   a second model column would be a speculation with no consumer.';

-- RLS: same shape as every other team-scoped table (org AND team, FORCE, anon denied).
ALTER TABLE public.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.agents;
CREATE POLICY tenant_isolation ON public.agents
  USING (app_private.row_visible(org_id, team_id))
  WITH CHECK (app_private.row_visible(org_id, team_id));
REVOKE ALL ON public.agents FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.agents TO authenticated;
-- Explicit, not inherited: 001's GRANT ALL ON ALL TABLES only covered tables that existed then,
-- and default-privileges only fire for objects the migrating role creates afterwards.
GRANT ALL ON public.agents TO service_role;

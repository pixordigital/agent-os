-- 004_agent_events.sql — agent_events: the operational timeline behind the org chart.
--
-- Append-only like audit_logs, but operational rather than probative: every state change worth
-- showing on the org chart or in a team timeline lands here (admin actions today, worker
-- heartbeats and task transitions in Wave 2). The org chart reads this table; it never
-- reconstructs history from current state, because current state cannot tell you that an
-- agent was blocked for two hours yesterday.
--
-- Realtime broadcast of these rows arrives with the full Supabase stack (realtime container,
-- Wave 2+). Until then the chart polls; the table is the contract either way.

CREATE TABLE IF NOT EXISTS public.agent_events (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id     uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  agent_id    uuid REFERENCES public.agents(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN (
                'created','updated','status_changed',
                'task_assigned','task_started','task_completed','task_failed',
                'heartbeat','model_swapped',
                'alert_opened','alert_resolved','hitl_pending','note')),
  payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_events_agent_time_idx
  ON public.agent_events (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_events_org_time_idx
  ON public.agent_events (org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_events_team_time_idx
  ON public.agent_events (team_id, created_at DESC);
COMMENT ON TABLE public.agent_events IS
  'Operational timeline, append-only. agent_id is ON DELETE CASCADE on purpose: these rows
   describe a live agent, not a legal record (that is audit_logs, which never cascades).';

-- RLS: same shape as every other team-scoped table.
ALTER TABLE public.agent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.agent_events;
CREATE POLICY tenant_isolation ON public.agent_events
  USING (app_private.row_visible(org_id, team_id))
  WITH CHECK (app_private.row_visible(org_id, team_id));
REVOKE ALL ON public.agent_events FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.agent_events TO authenticated;
GRANT ALL ON public.agent_events TO service_role;

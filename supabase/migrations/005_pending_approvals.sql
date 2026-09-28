-- 005_pending_approvals.sql — HITL queue: every human decision the system waits on.
--
-- One OPEN approval per task (partial unique index): a task that needs a second human decision
-- gets it after the first resolves, never concurrently — two open approvals on the same task is
-- how an approver says yes to a question that was already superseded.
--
-- Resolution is attributed to a PERSON (resolved_by_person_id), never to an agent: approval
-- authority is the one thing that must never be confusable between human and model (see
-- people table comment in 001). can_decide() in the app layer is the gate; this table is the
-- record.

CREATE TABLE IF NOT EXISTS public.pending_approvals (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id                uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id               uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  task_id               uuid NOT NULL REFERENCES public.tasks(id) ON DELETE CASCADE,
  kind                  text NOT NULL DEFAULT 'hitl' CHECK (kind IN ('hitl','review')),
  title                 text NOT NULL DEFAULT '',
  payload               jsonb NOT NULL DEFAULT '{}'::jsonb,
  status                text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','approved','rejected','expired','cancelled')),
  requested_by_agent_id uuid,
  resolved_by_person_id uuid REFERENCES public.people(id) ON DELETE SET NULL,
  resolution_note       text NOT NULL DEFAULT '',
  expires_at            timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  resolved_at           timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_approvals_open_uniq
  ON public.pending_approvals (task_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS pending_approvals_org_time_idx
  ON public.pending_approvals (org_id, created_at DESC);
COMMENT ON TABLE public.pending_approvals IS
  'Human decisions the system waits on. Only people resolve; agents request. The Slack HITL
   card and the dashboard approve button are two faces of the same row.';

-- RLS: same shape as every other team-scoped table.
ALTER TABLE public.pending_approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.pending_approvals FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.pending_approvals;
CREATE POLICY tenant_isolation ON public.pending_approvals
  USING (app_private.row_visible(org_id, team_id))
  WITH CHECK (app_private.row_visible(org_id, team_id));
REVOKE ALL ON public.pending_approvals FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.pending_approvals TO authenticated;
GRANT ALL ON public.pending_approvals TO service_role;

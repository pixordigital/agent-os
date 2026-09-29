-- 008_goals_kpis.sql — monthly financial goals + per-agent KPI values.
--
-- Two facts the platform could not state before: what the month must yield (goals,
-- set by the owner, changeable every month) and what each agent actually delivered
-- (kpi_values, measured or manual). kpi_defs is the closed vocabulary: an open text
-- key column would turn every KPI consumer into a string-matching lottery (see 004).
--
-- realized_brl on goals is MANUAL on purpose: revenue truth lives in the owner's
-- CRM/bank, not in agent_runs. The UI must show its source; a realized number without
-- a source is the exact lie AGENTS.md forbids.
-- Idempotent like the rest: IF NOT EXISTS everywhere, DROP POLICY first.

CREATE TABLE IF NOT EXISTS public.kpi_defs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id         uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id        uuid REFERENCES public.teams(id) ON DELETE CASCADE,
  key            text NOT NULL,
  name           text NOT NULL,
  unit           text NOT NULL DEFAULT '',
  target_monthly numeric(12,2),
  source         text NOT NULL DEFAULT 'manual' CHECK (source IN ('auto','manual')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, key)
);
COMMENT ON TABLE public.kpi_defs IS
  'Closed KPI vocabulary per org. source=auto values are computed live from agent_runs
   (runs, success_rate, cost_brl) and never stored; source=manual values land in
   kpi_values with an explicit source mark.';

CREATE TABLE IF NOT EXISTS public.kpi_values (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id     uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id    uuid REFERENCES public.teams(id) ON DELETE CASCADE,
  def_id     uuid NOT NULL REFERENCES public.kpi_defs(id) ON DELETE CASCADE,
  agent_id   uuid REFERENCES public.agents(id) ON DELETE SET NULL,
  month      text NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
  value      numeric(14,4) NOT NULL DEFAULT 0,
  source     text NOT NULL DEFAULT 'manual' CHECK (source IN ('auto','manual')),
  created_at timestamptz NOT NULL DEFAULT now()
);
-- One row per (def, agent-or-team, month): NULL agent_id means the team/org aggregate,
-- and NULL never compares equal, so the key coalesces it to the zero uuid.
CREATE UNIQUE INDEX IF NOT EXISTS kpi_values_uniq
  ON public.kpi_values (def_id, month,
                        COALESCE(agent_id, '00000000-0000-0000-0000-000000000000'::uuid));
CREATE INDEX IF NOT EXISTS kpi_values_agent_month_idx
  ON public.kpi_values (agent_id, month);
COMMENT ON TABLE public.kpi_values IS
  'Manual KPI readings. agent_id NULL = aggregate row for the team/org. Cost and run
   history stay in agent_runs; this table holds what only a human can state (revenue,
   deals, qualitative scores).';

CREATE TABLE IF NOT EXISTS public.goals (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id      uuid REFERENCES public.teams(id) ON DELETE CASCADE,
  month        text NOT NULL CHECK (month ~ '^[0-9]{4}-[0-9]{2}$'),
  target_brl   numeric(12,2) NOT NULL CHECK (target_brl >= 0),
  realized_brl numeric(12,2) NOT NULL DEFAULT 0 CHECK (realized_brl >= 0),
  note         text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);
-- One goal per (team-or-org, month): team_id NULL is the company goal.
CREATE UNIQUE INDEX IF NOT EXISTS goals_uniq
  ON public.goals (org_id, month,
                   COALESCE(team_id, '00000000-0000-0000-0000-000000000000'::uuid));
COMMENT ON TABLE public.goals IS
  'Monthly financial goals, set by the owner and changeable every month (upsert by
   month). team_id NULL = company-wide goal. realized_brl is manual CRM/bank truth.';

-- RLS: same shape as every other team-scoped table (org AND team, FORCE, anon denied).
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['kpi_defs','kpi_values','goals'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I
         USING (app_private.row_visible(org_id, team_id))
         WITH CHECK (app_private.row_visible(org_id, team_id))', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO authenticated', t);
    EXECUTE format('GRANT ALL ON public.%I TO service_role', t);
  END LOOP;
END;
$$;

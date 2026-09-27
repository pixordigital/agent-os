-- 001_core.sql — AgentOS kernel: tenant tree, task tree, policy engine, run accounting.
--
-- Idempotent on purpose: app/db.py applies this on every boot and Coolify restarts containers
-- freely. Every statement must be safe to re-run.
--
-- ORDERING MATTERS. `LANGUAGE sql` function bodies are validated at creation time, so any
-- helper that references a table or another function must come after it. This file is
-- therefore: extensions -> tables -> triggers -> RLS helpers -> RLS policies.
--
-- ENUM vs CHECK: no CREATE TYPE, because Postgres has no `CREATE TYPE IF NOT EXISTS`. CHECK
-- constraints keep migrations re-runnable, which matters more than enum ergonomics.
--
-- SECURITY MODEL (self-hosted Supabase):
--   * Browser / PostgREST read as `anon` or `authenticated` -> RLS applies, always.
--   * Server-side work (lease claim, state transitions, migrations) connects straight to
--     Postgres as the owner role. RLS is enabled AND FORCEd, so the owner is subject to policy
--     too; the app sets `app.current_org_id` / `app.current_person_id` per transaction.
--   * `service_role` carries BYPASSRLS. It is a server secret and must never reach a browser.
--   * `app_private` is not in PostgREST's exposed schemas, so its helpers are unreachable from
--     the outside even though RLS policies call them.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS app_private;
COMMENT ON SCHEMA app_private IS
  'Internal helpers. Not exposed through PostgREST, so nothing here is reachable from a browser.';

-- =====================================================================================
-- Tenant tree
-- =====================================================================================

CREATE TABLE IF NOT EXISTS public.organizations (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name       text NOT NULL,
  slug       text NOT NULL UNIQUE,
  settings   jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_active  boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.teams (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  key           text NOT NULL,
  name          text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, key)
);
COMMENT ON TABLE public.teams IS
  'The RLS scope unit. A team lead sees his team and not the next one, so this is the row the
   visibility predicate keys on — not merely a label.';

CREATE TABLE IF NOT EXISTS public.departments (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id          uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  key              text NOT NULL,
  name             text NOT NULL,
  mission          text NOT NULL DEFAULT '',
  risk_class       text NOT NULL DEFAULT 'baixo' CHECK (risk_class IN ('baixo','medio','alto','critico')),
  autonomy_ceiling smallint NOT NULL DEFAULT 0 CHECK (autonomy_ceiling BETWEEN 0 AND 3),
  budget_brl_month numeric(12,2) NOT NULL DEFAULT 0 CHECK (budget_brl_month >= 0),
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, key)
);
COMMENT ON COLUMN public.departments.autonomy_ceiling IS
  'L0 copilot, L1 supervised, L2 autonomous with review, L3 autonomous. No agent may exceed the
   ceiling of its department, whatever any policy says.';

CREATE TABLE IF NOT EXISTS public.people (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id     uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id    uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  name       text NOT NULL,
  email      text,
  kind       text NOT NULL DEFAULT 'human' CHECK (kind IN ('human')),
  status     text NOT NULL DEFAULT 'invited' CHECK (status IN ('active','invited','disabled')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS people_org_email_uniq
  ON public.people (org_id, lower(email)) WHERE email IS NOT NULL;
COMMENT ON TABLE public.people IS
  'Humans only. Agents are rows in agents (Wave 5) and never appear here, because approval
   authority and audit attribution must not be confusable between a person and a model.';

CREATE TABLE IF NOT EXISTS public.org_roles (
  person_id  uuid NOT NULL REFERENCES public.people(id) ON DELETE CASCADE,
  org_id     uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  role       text NOT NULL CHECK (role IN ('ceo','team_lead','member','viewer')),
  team_id    uuid REFERENCES public.teams(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now()
);
-- COALESCE in the key: team_id is NULL for org-level roles and NULL never compares equal in a
-- unique index, so without this a second ceo row would slip in.
CREATE UNIQUE INDEX IF NOT EXISTS org_roles_uniq
  ON public.org_roles (person_id, org_id, role,
                       COALESCE(team_id, '00000000-0000-0000-0000-000000000000'::uuid));
COMMENT ON TABLE public.org_roles IS
  'Only ceo carries approval authority. team_lead is consultive by decision: he commands,
   prioritises, delegates and pauses his own team, and approves nothing.';

CREATE TABLE IF NOT EXISTS public.slack_links (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id       uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  person_id     uuid NOT NULL REFERENCES public.people(id) ON DELETE CASCADE,
  slack_user_id text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, slack_user_id)
);
COMMENT ON TABLE public.slack_links IS
  'Routing only. A Slack user id is never an authorisation: authority comes from org_roles.
   team_id is denormalised from people.team_id on purpose — the visibility predicate needs a
   team column here, and resolving "who spoke" to "whose team" is the whole point.';

-- =====================================================================================
-- Task tree — the single source of truth
-- =====================================================================================

CREATE TABLE IF NOT EXISTS public.tasks (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id              uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id             uuid REFERENCES public.teams(id) ON DELETE SET NULL,  -- NULL = org level
  department_id       uuid REFERENCES public.departments(id) ON DELETE SET NULL,
  parent_task_id      uuid REFERENCES public.tasks(id) ON DELETE CASCADE,
  idempotency_key     text NOT NULL,
  version             integer NOT NULL DEFAULT 1,
  type                text NOT NULL DEFAULT 'generic',
  payload             jsonb NOT NULL DEFAULT '{}'::jsonb,
  risk                text NOT NULL DEFAULT 'baixo' CHECK (risk IN ('baixo','medio','alto','critico')),
  priority            integer NOT NULL DEFAULT 0,
  status              text NOT NULL DEFAULT 'received'
                        CHECK (status IN ('received','triaged','queued','assigned','leased','running',
                                          'verifying','waiting_human','retrying','rejected',
                                          'completed','failed','expired','cancelled')),
  assigned_team_id    uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  assigned_agent_id   uuid,  -- FK added in Wave 5, when agents exists
  acceptance_criteria jsonb NOT NULL DEFAULT '[]'::jsonb,
  verification        jsonb NOT NULL DEFAULT '{}'::jsonb,
  result              jsonb NOT NULL DEFAULT '{}'::jsonb,
  error               text NOT NULL DEFAULT '',
  attempt             integer NOT NULL DEFAULT 0,
  max_attempts        integer NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
  handoffs            integer NOT NULL DEFAULT 0 CHECK (handoffs <= 5),
  delegations         integer NOT NULL DEFAULT 0 CHECK (delegations <= 3),
  budget_brl          numeric(12,2) CHECK (budget_brl >= 0),
  sla_deadline        timestamptz,
  lease_until         timestamptz,
  lease_owner         text,
  last_progress_at    timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, idempotency_key)
);
-- Partial index: the claim query only ever looks at the statuses a worker can pick up.
CREATE INDEX IF NOT EXISTS tasks_claim_idx
  ON public.tasks (status, lease_until) WHERE status IN ('queued','leased','running');
CREATE INDEX IF NOT EXISTS tasks_org_team_idx ON public.tasks (org_id, team_id);
CREATE INDEX IF NOT EXISTS tasks_parent_idx   ON public.tasks (parent_task_id);
CREATE INDEX IF NOT EXISTS tasks_sla_idx ON public.tasks (org_id, sla_deadline)
  WHERE status NOT IN ('completed','failed','expired','cancelled');
COMMENT ON TABLE public.tasks IS
  'Single source of truth for all work; Redis only accelerates. parent_task_id forms the
   orchestrator -> manager -> agent tree. The handoffs/delegations ceilings are CHECK
   constraints on purpose: an unbounded delegation loop is the number one runaway cost.';
COMMENT ON COLUMN public.tasks.acceptance_criteria IS
  'What must be true for done. Deterministic checks run first; the LLM judge verifies only the
   semantic criteria and must cite the output line satisfying each one.';
COMMENT ON COLUMN public.tasks.verification IS
  '{method, result, passed, checked_at, by_person_id, by_agent_id, citations[]}. A verdict
   without a citation counts as a rejection, by design.';

CREATE TABLE IF NOT EXISTS public.task_transitions (
  from_status text NOT NULL,
  to_status   text NOT NULL,
  PRIMARY KEY (from_status, to_status)
);

INSERT INTO public.task_transitions (from_status, to_status) VALUES
  ('received',      'triaged'),     ('received',      'cancelled'),
  ('triaged',       'queued'),      ('triaged',       'cancelled'),
  ('queued',        'assigned'),    ('queued',        'cancelled'),
  ('assigned',      'leased'),      ('assigned',      'queued'),
  ('leased',        'running'),     ('leased',        'queued'),
  ('running',       'verifying'),   ('running',       'waiting_human'),
  ('running',       'retrying'),    ('running',       'completed'),
  ('running',       'failed'),      ('running',       'cancelled'),
  ('verifying',     'completed'),   ('verifying',     'rejected'),
  ('verifying',     'failed'),
  ('rejected',      'retrying'),    ('rejected',      'failed'),
  ('retrying',      'queued'),      ('retrying',      'failed'),
  ('waiting_human', 'queued'),      ('waiting_human', 'cancelled'),
  ('waiting_human', 'failed')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION app_private.guard_task_update() RETURNS trigger
  LANGUAGE plpgsql AS $$
DECLARE
  allowed boolean;
BEGIN
  -- The state machine lives in the database, not in application code: an app that forgets a
  -- rule must not be able to skip it.
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    SELECT EXISTS (SELECT 1 FROM public.task_transitions
                   WHERE from_status = OLD.status AND to_status = NEW.status) INTO allowed;
    IF NOT allowed THEN
      RAISE EXCEPTION 'invalid task transition % -> % for task %', OLD.status, NEW.status, OLD.id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  -- Optimistic concurrency: whoever wrote a stale version loses.
  IF NEW.version <> OLD.version + 1 THEN
    RAISE EXCEPTION 'stale task version %: expected %, got %', OLD.id, OLD.version + 1, NEW.version
      USING ERRCODE = 'serialization_failure';
  END IF;

  -- Self-parenting or a cycle would make the tree unbounded and the org chart a lie.
  IF NEW.parent_task_id IS NOT NULL THEN
    IF NEW.parent_task_id = NEW.id THEN
      RAISE EXCEPTION 'task % cannot be its own parent', NEW.id USING ERRCODE = 'check_violation';
    END IF;
    IF EXISTS (
      WITH RECURSIVE up AS (
        SELECT id, parent_task_id FROM public.tasks WHERE id = NEW.parent_task_id
        UNION ALL
        SELECT t.id, t.parent_task_id FROM public.tasks t JOIN up ON t.id = up.parent_task_id
      )
      SELECT 1 FROM up WHERE id = NEW.id
    ) THEN
      RAISE EXCEPTION 'parent_task_id % creates a cycle on task %', NEW.parent_task_id, NEW.id
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tasks_guard_update ON public.tasks;
CREATE TRIGGER tasks_guard_update BEFORE UPDATE ON public.tasks
  FOR EACH ROW EXECUTE FUNCTION app_private.guard_task_update();

-- =====================================================================================
-- Policy engine — the autonomy line
-- =====================================================================================

CREATE TABLE IF NOT EXISTS public.policies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id           uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  action           text NOT NULL,
  risk             text NOT NULL CHECK (risk IN ('baixo','medio','alto','critico')),
  decision         text NOT NULL CHECK (decision IN ('ALLOW','REQUIRE_HITL','DENY')),
  required_role    text,
  approval_timeout interval,
  fallback_action  text,
  business_impact  jsonb NOT NULL DEFAULT '{}'::jsonb,
  priority         integer NOT NULL DEFAULT 100,
  version          integer NOT NULL DEFAULT 1,
  status           text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','archived')),
  is_seed          boolean NOT NULL DEFAULT false,
  created_by       uuid REFERENCES public.people(id) ON DELETE SET NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS policies_active_uniq
  ON public.policies (org_id, action, version) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS policies_lookup_idx ON public.policies (org_id, action, status);
COMMENT ON TABLE public.policies IS
  'Autonomy is the default; the exception is this list. Precedence: org custom (is_seed=false) >
   vertical seed > lowest priority > default DENY+HITL. An action with no matching policy is
   fail-closed, never permissive.';

-- =====================================================================================
-- Run accounting — the only source of cost, tokens and degradation evidence
-- =====================================================================================

CREATE TABLE IF NOT EXISTS public.agent_runs (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id                uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  team_id               uuid REFERENCES public.teams(id) ON DELETE SET NULL,
  agent_id              uuid,  -- FK added in Wave 5, when agents exists
  task_id               uuid REFERENCES public.tasks(id) ON DELETE SET NULL,
  backend               text NOT NULL DEFAULT 'native',
  model                 text NOT NULL,
  prompt_version        text,
  success               boolean,
  retry_count           integer NOT NULL DEFAULT 0,
  format_violation      boolean NOT NULL DEFAULT false,
  latency_ms            integer,
  tokens_in             integer NOT NULL DEFAULT 0,
  tokens_out            integer NOT NULL DEFAULT 0,
  cached_tokens         integer NOT NULL DEFAULT 0,
  cache_creation_tokens integer NOT NULL DEFAULT 0,
  cost_brl              numeric(12,6) NOT NULL DEFAULT 0,
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_runs_agent_time_idx ON public.agent_runs (agent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS agent_runs_org_time_idx   ON public.agent_runs (org_id, created_at DESC);
COMMENT ON COLUMN public.agent_runs.cached_tokens IS
  'Provider-reported cache-read tokens. The dashboard shows measured cache hit rate and never an
   assumed saving percentage.';
COMMENT ON COLUMN public.agent_runs.cache_creation_tokens IS
  'Provider-reported cache-write tokens. Cache reads only pay off across a long session with a
   byte-stable prefix, which is why session length is a cost lever and not only a latency one.';

CREATE TABLE IF NOT EXISTS public.audit_logs (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          uuid NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
  actor_type      text NOT NULL CHECK (actor_type IN ('human','agent','system')),
  actor_person_id uuid REFERENCES public.people(id) ON DELETE SET NULL,
  actor_agent_id  uuid,
  action          text NOT NULL,
  before_state    jsonb,
  after_state     jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_logs_org_time_idx ON public.audit_logs (org_id, created_at DESC);
COMMENT ON TABLE public.audit_logs IS
  'Append-only, enforced below: a correction is a new row, never an edit. The value of this
   table is precisely that it cannot be rewritten.';

-- =====================================================================================
-- RLS helpers (after the tables they reference)
-- `true` as the second arg to current_setting means "missing setting yields NULL" instead of
-- raising, which is what makes fail-closed behaviour expressible.
-- =====================================================================================

CREATE OR REPLACE FUNCTION app_private.current_org_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_org_id', true), '')::uuid;
  $$;

CREATE OR REPLACE FUNCTION app_private.current_person_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_person_id', true), '')::uuid;
  $$;

CREATE OR REPLACE FUNCTION app_private.current_team_id() RETURNS uuid
  LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_team_id', true), '')::uuid;
  $$;

-- SECURITY DEFINER because the predicate has to read the caller's own org_roles row, which is
-- itself behind the very policy being evaluated. Without it, is_ceo() would recurse.
CREATE OR REPLACE FUNCTION app_private.is_ceo() RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app_private, public AS $$
    SELECT EXISTS (
      SELECT 1 FROM public.org_roles
      WHERE org_id    = app_private.current_org_id()
        AND person_id = app_private.current_person_id()
        AND role      = 'ceo'
    );
  $$;

-- One predicate for every org-scoped row.
--
-- team_id NULL means ORG-WIDE, and org-wide is visible to every member of the org — including
-- team leads. That is deliberate and it is the reason the task tree works: a manager must be
-- able to read its own parent task, which is org-level. The consequence is that an org-level
-- task is never private; a per-person ACL on tasks does not exist and would be the only way to
-- make it private. Do not assume otherwise in the UI.
--
-- No org setting set, no rows: fail-closed.
CREATE OR REPLACE FUNCTION app_private.row_visible(p_org_id uuid, p_team_id uuid)
  RETURNS boolean
  LANGUAGE sql STABLE AS $$
    SELECT p_org_id IS NOT NULL
       AND p_org_id = app_private.current_org_id()
       AND (p_team_id IS NULL
            OR app_private.is_ceo()
            OR p_team_id = app_private.current_team_id());
  $$;

-- is_ceo() reads org_roles, and org_roles' own policy calls is_ceo() so the owner can see every
-- role. Under FORCE RLS that is infinite recursion — found by running this against a real
-- Postgres, not by reading it. Owning the function by a BYPASSRLS role breaks the cycle, since
-- the read inside it is no longer subject to the policy that invoked it.
--
-- Two hard requirements for the ALTER to work, both found the loud way against supabase/postgres
-- (where the app role is NOT superuser, unlike plain Postgres):
--   1. the executor must be a member of service_role (bootstrap: GRANT service_role TO postgres);
--   2. service_role must hold CREATE on the schema, because the new owner needs it.
-- Guarded because plain Postgres (local dev) has no service_role, and there the cycle does not
-- arise for lack of FORCE.
GRANT CREATE ON SCHEMA app_private TO service_role;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    EXECUTE 'ALTER FUNCTION app_private.is_ceo() OWNER TO service_role';
  END IF;
END;
$$;

-- Policy expressions call these helpers, so the querying role needs USAGE on the schema. Without
-- it every single PostgREST query fails with "permission denied for schema app_private", which is
-- a loud failure — but only after the policies look correct in review.
--
-- Granting USAGE is safe: these functions return booleans derived from the caller's own session
-- settings, and `is_ceo()` tells the caller only what they could already see by reading their
-- own roles. The schema stays out of PostgREST's exposed list, so nothing is *listable*.
--
-- `anon` is on this list ON PURPOSE and it is the easiest line in this file to "fix" by
-- deleting. RLS policy expressions execute as the querying role, and an unauthenticated
-- PostgREST request runs as `anon` — without USAGE here, every anonymous read fails with
-- "permission denied for schema app_private" AFTER the JWT validated, which looks exactly
-- like a broken key and sends you debugging the wrong layer for an hour.
GRANT USAGE ON SCHEMA app_private TO anon, authenticated, service_role;

-- =====================================================================================
-- RLS: one policy per table, org AND team scoped
-- =====================================================================================

DO $$
DECLARE
  t text;
  team_scoped text[] := ARRAY['departments','people','slack_links','tasks','agent_runs'];
  org_scoped  text[] := ARRAY['policies','audit_logs'];
BEGIN
  FOREACH t IN ARRAY team_scoped LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I
         USING (app_private.row_visible(org_id, team_id))
         WITH CHECK (app_private.row_visible(org_id, team_id))', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO authenticated', t);
  END LOOP;

  FOREACH t IN ARRAY org_scoped LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON public.%I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON public.%I
         USING (org_id = app_private.current_org_id())
         WITH CHECK (org_id = app_private.current_org_id())', t);
    EXECUTE format('REVOKE ALL ON public.%I FROM anon', t);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO authenticated', t);
  END LOOP;

  -- teams has no team_id column: it IS the scope. A lead sees his own team row (and must, to
  -- render his own name in the UI); the owner sees all of them. That is row_visible(org, id).
  EXECUTE 'ALTER TABLE public.teams ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE public.teams FORCE ROW LEVEL SECURITY';
  EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON public.teams';
  EXECUTE 'CREATE POLICY tenant_isolation ON public.teams
             USING (app_private.row_visible(org_id, id))
             WITH CHECK (app_private.row_visible(org_id, id))';
  EXECUTE 'REVOKE ALL ON public.teams FROM anon';
  EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.teams TO authenticated';

  -- task_transitions is a static reference table: the state machine itself. It has no org_id and
  -- no tenant data, so it needs no RLS — but `anon` must still not read it, because the default
  -- grant would publish the whole transition graph to any unauthenticated browser.
  EXECUTE 'REVOKE ALL ON public.task_transitions FROM anon';
  EXECUTE 'GRANT SELECT ON public.task_transitions TO authenticated';

  -- organizations is keyed by its own primary key, so it does not fit either loop.
  EXECUTE 'ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY';
  EXECUTE 'ALTER TABLE public.organizations FORCE ROW LEVEL SECURITY';
  EXECUTE 'DROP POLICY IF EXISTS tenant_isolation ON public.organizations';
  EXECUTE 'CREATE POLICY tenant_isolation ON public.organizations
             USING (id = app_private.current_org_id())
             WITH CHECK (id = app_private.current_org_id())';
  EXECUTE 'REVOKE ALL ON public.organizations FROM anon';
  EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON public.organizations TO authenticated';
END;
$$;

-- org_roles: writes are org-scoped, reads are NOT.
--
-- Postgres ORs permissive policies together, so a single `FOR ALL` org policy would have made
-- the narrow read policy below meaningless — every member could enumerate the whole org's roles
-- and learn who holds which authority. That was a real leak, found by running the policies and
-- counting rows, not by reading them. Split by command so read and write have genuinely
-- different scopes.
ALTER TABLE public.org_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.org_roles FORCE ROW LEVEL SECURITY;
REVOKE ALL ON public.org_roles FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.org_roles TO authenticated;

-- CREATE POLICY has no IF NOT EXISTS in Postgres, so each is dropped first. Re-running this
-- migration is not optional: app/db.py applies it on every boot.
DROP POLICY IF EXISTS tenant_isolation ON public.org_roles;
DROP POLICY IF EXISTS own_roles ON public.org_roles;
DROP POLICY IF EXISTS org_roles_insert ON public.org_roles;
DROP POLICY IF EXISTS org_roles_update ON public.org_roles;
DROP POLICY IF EXISTS org_roles_delete ON public.org_roles;
DROP POLICY IF EXISTS org_roles_read ON public.org_roles;

CREATE POLICY org_roles_insert ON public.org_roles FOR INSERT
  WITH CHECK (org_id = app_private.current_org_id());
CREATE POLICY org_roles_update ON public.org_roles FOR UPDATE
  USING (org_id = app_private.current_org_id())
  WITH CHECK (org_id = app_private.current_org_id());
CREATE POLICY org_roles_delete ON public.org_roles FOR DELETE
  USING (org_id = app_private.current_org_id());
-- Read: your own roles, plus everything if you are the owner. is_ceo() is evaluated as a
-- BYPASSRLS role (see above), so this does not recurse through its own policy.
CREATE POLICY org_roles_read ON public.org_roles FOR SELECT
  USING (person_id = app_private.current_person_id() OR app_private.is_ceo());

REVOKE UPDATE, DELETE ON public.audit_logs FROM authenticated, anon;

-- service_role is the server-side role with BYPASSRLS. That flag skips the *policy* check, not
-- the *grant* check: a role with no GRANT gets "permission denied" even with bypass enabled.
-- Without these grants, server-side seeding, admin tooling and the orchestrator's own writes all
-- fail — which is the failure a trimmed stack (no official init scripts) hits first.
GRANT ALL ON ALL TABLES IN SCHEMA public TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;

-- PostgREST resolves table names through the schema, so every querying role needs USAGE on it.
-- USAGE only — never CREATE: Supabase deliberately keeps CREATE off `public` so that a
-- compromised tenant credential cannot create objects in the application schema. Without this,
-- every INSERT/SELECT as `authenticated` fails with "permission denied for schema public",
-- which is exactly what a trimmed stack (no official init scripts) hits on first use.
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role, authenticator;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO authenticated;
-- service_role gets the same future coverage: without it, every new table created by a later
-- migration would be unreadable server-side until someone noticed. Same rule, no drift.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO service_role;

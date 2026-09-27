# One-time bootstrap for a fresh database

The migration `../migrations/001_core.sql` is designed to run as the **app owner role**
(`postgres` in the trimmed stack, `DATABASE_URL`). On a brand-new database from the
`supabase/postgres` image, three things must be true before the first run, once:

```sql
-- 1. The `ALTER FUNCTION ... OWNER TO service_role` step needs membership.
GRANT service_role TO postgres;

-- 2. The app role must own the schema it migrates into.
ALTER SCHEMA public OWNER TO postgres;

-- 3. PostgREST connects as `authenticator`; the image creates the role but leaves its
--    password empty. PostgREST cannot log in until this is set (same value the compose
--    passes as PGRST_DB_URI password, i.e. the Supabase convention).
ALTER ROLE authenticator WITH PASSWORD '<POSTGRES_PASSWORD>';
```

Why these are manual and not in the migration: granting to yourself (`GRANT service_role TO
postgres` inside a migration run by `postgres`) only works when the executor can grant —
`postgres` cannot grant membership in a role it is not yet a member of. The migration
documents its own prerequisites instead of failing halfway.

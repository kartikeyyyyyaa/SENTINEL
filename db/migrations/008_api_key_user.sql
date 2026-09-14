-- 008_api_key_user.sql — give every machine API key an app_user to hang
-- permissions off, so a machine caller is authorised through exactly the same
-- path as a human one.
--
-- The problem this closes: 004_rls.sql's has_permission() / is_statewide() /
-- current_jurisdiction_path() all resolve through app.user_role, keyed on
-- app.current_user_id() — the one claim the application ever asserts. But
-- app.api_key (001_core.sql) carries its own department_id/jurisdiction_id/
-- role_id and no link to app.app_user at all. Taken literally, a caller
-- authenticated only by API key has no app.user_id to assert, so
-- app.has_permission() returns false for it and it can insert nothing —
-- including the watchlist_match and alert rows the analytics worker's
-- AlertHttpTransport exists to send (007_watchlist.sql's INSERT policies both
-- gate on has_permission('watchlist.read') or ('camera.health_write')).
--
-- Rather than teach the RLS functions a second, parallel notion of "caller" —
-- which would mean every future policy remembers to check two paths instead of
-- one — each API key gets a machine app_user row (is_active, an unusable
-- password hash, no login route ever issues it a session) with exactly the
-- role app.api_key.role_id already named. Authenticating an API key then means:
-- look up the key by prefix, verify the hash, and set_config('app.user_id', ...)
-- to its user_id, precisely as auth.py does after a password check. One
-- security context implementation, two ways to arrive at a user_id.
ALTER TABLE app.api_key ADD COLUMN user_id bigint REFERENCES app.app_user(id) ON DELETE CASCADE;

COMMENT ON COLUMN app.api_key.user_id IS
    'The machine app_user this key authenticates as. RLS derives all '
    'permissions/scope from this user''s roles, exactly as it does for a '
    'human login — see api/deps.py''s api-key dependency.';

CREATE INDEX api_key_user_idx ON app.api_key (user_id);


-- ---------------------------------------------------------------------------
-- A machine role for the analytics edge worker, distinct from 'integrator'
-- ---------------------------------------------------------------------------
-- 'integrator' (005_reference_data.sql) is scoped to camera onboarding —
-- camera.create/update/health_write — and deliberately holds no watchlist or
-- alert permission. The edge worker is a different machine principal with a
-- different job: it never onboards a camera, but it does need to pull the
-- active watchlist (GET /api/analytics/watchlist) and post the matches/alerts
-- that pull makes possible. Reusing 'integrator' for that would mean widening
-- a camera-onboarding credential with watchlist read access, or widening this
-- one with camera-onboarding rights it has no business holding — either way,
-- one compromised key would reach further than its actual job requires.
INSERT INTO app.role (code, name, description, is_statewide) VALUES
    ('edge_worker', 'Analytics Edge Worker (machine)',
     'Machine role for an edge analytics worker: pulls the active watchlist and '
     'reports matches/alerts. Holds no camera-onboarding or interactive-user '
     'permission.', false)
ON CONFLICT (code) DO UPDATE
    SET name = EXCLUDED.name, description = EXCLUDED.description;

-- camera.read_cross_department, too: an edge box is deployed to wherever its
-- physical cameras sit, which routinely spans more than one department (a
-- municipal traffic ANPR unit and a police dome on the same street, say) —
-- see services/registry/app/api/routers/analytics.py's alert ingestion, which
-- needs to read a camera's jurisdiction regardless of which department owns
-- it in order to stamp an incoming alert with the right jurisdiction_path.
--
-- alert.read, too — and this one is easy to miss. Postgres RLS requires a row
-- to satisfy the table's SELECT policy before it can appear in an INSERT's
-- RETURNING list, not merely the INSERT policy's WITH CHECK; without
-- alert.read, `INSERT INTO app.alert (...) RETURNING ...` fails with the same
-- generic "new row violates row-level security policy" error a WITH CHECK
-- failure would, even though the insert itself was permitted. The alert
-- ingestion endpoint also SELECTs an existing open alert by alert_key before
-- deciding whether to insert or update it (its upsert-by-alert_key semantics
-- — see analytics.py's module docstring); without alert.read that SELECT
-- would silently see nothing, and every delivery would look like a fresh
-- alert instead of an update to one already open.
INSERT INTO app.role_permission (role_id, permission_code)
SELECT r.id, p.code
FROM   app.role r
JOIN   (VALUES ('watchlist.read'), ('camera.health_write'),
               ('camera.read_cross_department'), ('alert.read'))
           AS p(code) ON true
WHERE  r.code = 'edge_worker'
ON CONFLICT (role_id, permission_code) DO NOTHING;

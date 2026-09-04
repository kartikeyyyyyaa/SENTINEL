-- 004_rls.sql — Row-Level Security, derived authorisation, and least-privilege grants.
--
-- The central idea: the application's entire authorisation claim is a single
-- integer, `app.user_id`. Everything else — which jurisdiction subtree the user
-- may see, which department they belong to, which permissions they hold, whether
-- they are statewide — is derived inside the database by joining the identity
-- tables. A compromised application cannot widen its own scope by asserting a
-- bigger claim, because there is no bigger claim to assert.
--
-- All helpers are STABLE, so Postgres evaluates them once per statement rather
-- than once per row.

-- ---------------------------------------------------------------------------
-- Denormalised jurisdiction path on camera
-- ---------------------------------------------------------------------------
-- The RLS predicate is a prefix test. Doing it against a column on the row
-- itself keeps it index-friendly; resolving jurisdiction_id -> path per row
-- inside the policy would not be. Kept in sync by trigger, never written by the
-- application.
ALTER TABLE app.camera ADD COLUMN jurisdiction_path text;

CREATE OR REPLACE FUNCTION app.camera_sync_jurisdiction_path() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    SELECT j.path INTO NEW.jurisdiction_path
    FROM   app.jurisdiction j
    WHERE  j.id = NEW.jurisdiction_id;

    IF NEW.jurisdiction_path IS NULL THEN
        RAISE EXCEPTION 'jurisdiction_id % does not exist', NEW.jurisdiction_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER camera_sync_path
    BEFORE INSERT OR UPDATE OF jurisdiction_id ON app.camera
    FOR EACH ROW EXECUTE FUNCTION app.camera_sync_jurisdiction_path();

-- If a jurisdiction is ever re-parented, cascade the new path to its cameras.
CREATE OR REPLACE FUNCTION app.jurisdiction_cascade_path() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.path IS DISTINCT FROM OLD.path THEN
        UPDATE app.jurisdiction
           SET path = NEW.path || substring(path from length(OLD.path) + 1)
         WHERE path LIKE OLD.path || '.%';

        UPDATE app.camera c
           SET jurisdiction_path = j.path
          FROM app.jurisdiction j
         WHERE c.jurisdiction_id = j.id
           AND (j.path = NEW.path OR j.path LIKE NEW.path || '.%');
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER jurisdiction_cascade
    AFTER UPDATE OF path ON app.jurisdiction
    FOR EACH ROW EXECUTE FUNCTION app.jurisdiction_cascade_path();

CREATE INDEX camera_jurisdiction_path_idx ON app.camera (jurisdiction_path text_pattern_ops);


-- ---------------------------------------------------------------------------
-- Derived security context
-- ---------------------------------------------------------------------------
-- The one thing the application asserts.
CREATE OR REPLACE FUNCTION app.current_user_id() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')::bigint
$$;

-- SECURITY DEFINER: these read identity tables that the app role has no direct
-- access to. search_path is pinned so the function body cannot be hijacked by a
-- caller-controlled search_path.
CREATE OR REPLACE FUNCTION app.current_jurisdiction_path() RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, public AS $$
    SELECT j.path
    FROM   app.app_user u
    JOIN   app.jurisdiction j ON j.id = u.jurisdiction_id
    WHERE  u.id = app.current_user_id()
      AND  u.is_active
$$;

CREATE OR REPLACE FUNCTION app.current_department_id() RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, public AS $$
    SELECT u.department_id
    FROM   app.app_user u
    WHERE  u.id = app.current_user_id()
      AND  u.is_active
$$;

CREATE OR REPLACE FUNCTION app.has_permission(perm text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, public AS $$
    SELECT EXISTS (
        SELECT 1
        FROM   app.app_user u
        JOIN   app.user_role ur      ON ur.user_id = u.id
        JOIN   app.role_permission rp ON rp.role_id = ur.role_id
        WHERE  u.id = app.current_user_id()
          AND  u.is_active
          AND  rp.permission_code = perm
    )
$$;

CREATE OR REPLACE FUNCTION app.is_statewide() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = app, public AS $$
    SELECT EXISTS (
        SELECT 1
        FROM   app.app_user u
        JOIN   app.user_role ur ON ur.user_id = u.id
        JOIN   app.role r       ON r.id = ur.role_id
        WHERE  u.id = app.current_user_id()
          AND  u.is_active
          AND  r.is_statewide
    )
$$;

-- Is a jurisdiction path inside the caller's subtree? Returns false (not NULL)
-- when there is no caller, so policies fail closed.
CREATE OR REPLACE FUNCTION app.path_in_scope(target_path text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN app.current_jurisdiction_path() IS NULL OR target_path IS NULL THEN false
        WHEN target_path = app.current_jurisdiction_path() THEN true
        ELSE target_path LIKE app.current_jurisdiction_path() || '.%'
    END
$$;

COMMENT ON FUNCTION app.path_in_scope(text) IS
    'Subtree containment. Safe against LIKE metacharacters because '
    'jurisdiction.path is constrained to ^[A-Z0-9-]+(\.[A-Z0-9-]+)*$.';


-- ---------------------------------------------------------------------------
-- Camera policies
-- ---------------------------------------------------------------------------
ALTER TABLE app.camera ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.camera FORCE ROW LEVEL SECURITY;

-- Read: inside your jurisdiction subtree, and either your own department or an
-- explicit cross-department permission. Statewide roles skip the subtree test.
CREATE POLICY camera_select ON app.camera FOR SELECT
USING (
    app.is_statewide()
    OR (
        app.path_in_scope(jurisdiction_path)
        AND (
            app.has_permission('camera.read_cross_department')
            OR department_id = app.current_department_id()
        )
    )
);

CREATE POLICY camera_insert ON app.camera FOR INSERT
WITH CHECK (
    app.has_permission('camera.create')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY camera_update ON app.camera FOR UPDATE
USING (
    app.has_permission('camera.update')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
)
WITH CHECK (
    app.has_permission('camera.update')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY camera_delete ON app.camera FOR DELETE
USING (
    app.has_permission('camera.delete')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);


-- ---------------------------------------------------------------------------
-- Health history follows camera visibility
-- ---------------------------------------------------------------------------
-- RLS composes: because app.camera has RLS, this EXISTS only finds cameras the
-- caller is already allowed to see. No duplicated predicate to drift out of sync.
ALTER TABLE app.camera_health_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.camera_health_check FORCE ROW LEVEL SECURITY;

CREATE POLICY health_select ON app.camera_health_check FOR SELECT
USING (EXISTS (SELECT 1 FROM app.camera c WHERE c.id = camera_id));

CREATE POLICY health_insert ON app.camera_health_check FOR INSERT
WITH CHECK (app.has_permission('camera.health_write'));


-- ---------------------------------------------------------------------------
-- Credential vault: no direct access at all
-- ---------------------------------------------------------------------------
-- RLS is enabled with no permissive SELECT policy, so the table is invisible to
-- the application role no matter what query it runs. The only way in is the
-- SECURITY DEFINER accessor below, which checks an explicit permission and
-- leaves an audit trail via the caller.
ALTER TABLE app.camera_credential ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.camera_credential FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION app.get_camera_credential(p_camera_id bigint)
RETURNS TABLE (username_enc bytea, password_enc bytea, wrapped_dek bytea, key_version integer)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
    IF NOT app.has_permission('camera.credential_use') THEN
        RAISE EXCEPTION 'permission denied: camera.credential_use'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Re-check camera visibility through the RLS-protected table.
    IF NOT EXISTS (SELECT 1 FROM app.camera c WHERE c.id = p_camera_id) THEN
        RAISE EXCEPTION 'camera not visible to caller'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    RETURN QUERY
        SELECT cc.username_enc, cc.password_enc, cc.wrapped_dek, cc.key_version
        FROM   app.camera_credential cc
        WHERE  cc.camera_id = p_camera_id;
END $$;


-- ---------------------------------------------------------------------------
-- Audit policies
-- ---------------------------------------------------------------------------
ALTER TABLE app.audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.audit_log FORCE ROW LEVEL SECURITY;

-- Anything may be written; that is the point of an audit trail.
CREATE POLICY audit_insert ON app.audit_log FOR INSERT WITH CHECK (true);

-- Auditors read everything. Everyone else can always read their own actions,
-- which makes the trail a transparency mechanism as well as an oversight one.
CREATE POLICY audit_select ON app.audit_log FOR SELECT
USING (
    app.has_permission('audit.read')
    OR actor_user_id = app.current_user_id()
);


-- ---------------------------------------------------------------------------
-- Import batches
-- ---------------------------------------------------------------------------
ALTER TABLE app.import_batch ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.import_batch FORCE ROW LEVEL SECURITY;

CREATE POLICY import_select ON app.import_batch FOR SELECT
USING (app.has_permission('camera.create') OR uploaded_by = app.current_user_id());

CREATE POLICY import_insert ON app.import_batch FOR INSERT
WITH CHECK (app.has_permission('camera.create'));

CREATE POLICY import_update ON app.import_batch FOR UPDATE
USING (uploaded_by = app.current_user_id())
WITH CHECK (uploaded_by = app.current_user_id());


-- ---------------------------------------------------------------------------
-- Grants: least privilege, enumerated explicitly
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    app_role text := current_setting('sentinel.app_role', true);
BEGIN
    IF app_role IS NULL OR app_role = '' THEN
        app_role := 'sentinel_app';
    END IF;

    EXECUTE format('GRANT USAGE ON SCHEMA app TO %I', app_role);

    -- Reference data: read-only.
    EXECUTE format('GRANT SELECT ON app.jurisdiction, app.department, app.role, '
                   'app.permission, app.role_permission TO %I', app_role);

    -- Identity: the API needs to read users to authenticate them and update
    -- lockout counters and last-login markers. It may not create roles.
    EXECUTE format('GRANT SELECT, UPDATE ON app.app_user TO %I', app_role);
    EXECUTE format('GRANT SELECT ON app.user_role TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.refresh_token TO %I', app_role);
    EXECUTE format('GRANT SELECT, UPDATE ON app.api_key TO %I', app_role);

    -- Operational tables.
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON app.camera TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT ON app.camera_health_check TO %I', app_role);
    EXECUTE format('GRANT SELECT ON app.camera_health_current TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.import_batch TO %I', app_role);

    -- Audit: append and read only. No UPDATE, no DELETE, ever.
    EXECUTE format('GRANT SELECT, INSERT ON app.audit_log TO %I', app_role);

    -- Credentials: no table privileges at all. Access is via the accessor
    -- function only.
    EXECUTE format('REVOKE ALL ON app.camera_credential FROM %I', app_role);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app.get_camera_credential(bigint) TO %I', app_role);

    -- Sequences for the tables the app inserts into.
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.camera_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.camera_health_check_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.audit_log_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.import_batch_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.refresh_token_id_seq TO %I', app_role);

    -- Verification is a read-only diagnostic; let the API expose it.
    EXECUTE format('GRANT EXECUTE ON FUNCTION app.verify_audit_chain(bigint, integer) TO %I', app_role);
END $$;

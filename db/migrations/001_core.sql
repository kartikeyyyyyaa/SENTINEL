-- 001_core.sql — extensions, jurisdiction hierarchy, departments, identity.
--
-- Jurisdictions are stored as a hierarchy with a materialised dot-separated path
-- ('GJ.AHM.Z1.PS-NAVRANGPURA'). Subtree containment then becomes a plain indexed
-- prefix test, which is what the Row-Level Security policies use on every read.
-- The '.' terminator in the LIKE pattern prevents 'GJ.AHM' from matching
-- 'GJ.AHMEDNAGAR'.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- PostGIS and pgcrypto functions live in public; the app role needs to reach
-- them but must never create objects there.
GRANT USAGE ON SCHEMA public TO PUBLIC;

-- Generic "touch updated_at" trigger, used by several tables below.
CREATE OR REPLACE FUNCTION app.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;


-- ---------------------------------------------------------------------------
-- Jurisdiction hierarchy
-- ---------------------------------------------------------------------------
CREATE TABLE app.jurisdiction (
    id          bigserial PRIMARY KEY,
    code        text NOT NULL UNIQUE,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN (
                    'state', 'range', 'district', 'city', 'zone',
                    'division', 'police_station', 'ward'
                )),
    parent_id   bigint REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,
    -- Materialised path. Segments are [A-Z0-9-], joined by '.'.
    path        text NOT NULL UNIQUE
                    CHECK (path ~ '^[A-Z0-9-]+(\.[A-Z0-9-]+)*$'),
    boundary    geography(MultiPolygon, 4326),
    population  integer,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- text_pattern_ops so `path LIKE 'GJ.AHM.%'` can use the index.
CREATE INDEX jurisdiction_path_prefix_idx ON app.jurisdiction (path text_pattern_ops);
CREATE INDEX jurisdiction_parent_idx      ON app.jurisdiction (parent_id);
CREATE INDEX jurisdiction_boundary_idx    ON app.jurisdiction USING gist (boundary);
CREATE INDEX jurisdiction_kind_idx        ON app.jurisdiction (kind);

CREATE TRIGGER jurisdiction_touch BEFORE UPDATE ON app.jurisdiction
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

COMMENT ON COLUMN app.jurisdiction.path IS
    'Materialised ancestry path used for RLS subtree containment tests.';


-- ---------------------------------------------------------------------------
-- Departments — the "who owns this camera" axis, orthogonal to jurisdiction
-- ---------------------------------------------------------------------------
CREATE TABLE app.department (
    id            bigserial PRIMARY KEY,
    code          text NOT NULL UNIQUE,
    name          text NOT NULL,
    kind          text NOT NULL CHECK (kind IN (
                      'police', 'traffic', 'municipal', 'transport',
                      'institution', 'private', 'railway', 'other'
                  )),
    contact_name  text,
    contact_email text,
    contact_phone text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX department_kind_idx ON app.department (kind);

CREATE TRIGGER department_touch BEFORE UPDATE ON app.department
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Identity, roles, permissions
-- ---------------------------------------------------------------------------
CREATE TABLE app.permission (
    code        text PRIMARY KEY CHECK (code ~ '^[a-z_]+(\.[a-z_]+)+$'),
    description text NOT NULL
);

CREATE TABLE app.role (
    id          bigserial PRIMARY KEY,
    code        text NOT NULL UNIQUE CHECK (code ~ '^[a-z_]+$'),
    name        text NOT NULL,
    description text,
    -- A statewide role sees every jurisdiction, bypassing the subtree test but
    -- NOT the audit trail. There is no role that escapes being logged.
    is_statewide boolean NOT NULL DEFAULT false,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app.role_permission (
    role_id         bigint NOT NULL REFERENCES app.role(id) ON DELETE CASCADE,
    permission_code text   NOT NULL REFERENCES app.permission(code) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_code)
);

CREATE TABLE app.app_user (
    id                  bigserial PRIMARY KEY,
    username            text NOT NULL UNIQUE CHECK (length(username) BETWEEN 3 AND 64),
    full_name           text NOT NULL,
    email               text,
    phone               text,
    -- bcrypt hash. Never a plaintext or reversible representation.
    password_hash       text NOT NULL,
    -- TOTP secret, encrypted at rest with the same envelope scheme as camera
    -- credentials. NULL means MFA is not yet enrolled.
    mfa_secret_enc      bytea,
    mfa_enrolled        boolean NOT NULL DEFAULT false,
    department_id       bigint REFERENCES app.department(id) ON DELETE RESTRICT,
    jurisdiction_id     bigint NOT NULL REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,
    is_active           boolean NOT NULL DEFAULT true,
    failed_attempts     integer NOT NULL DEFAULT 0,
    locked_until        timestamptz,
    password_changed_at timestamptz NOT NULL DEFAULT now(),
    must_change_password boolean NOT NULL DEFAULT false,
    last_login_at       timestamptz,
    last_login_ip       inet,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          bigint REFERENCES app.app_user(id),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX app_user_department_idx   ON app.app_user (department_id);
CREATE INDEX app_user_jurisdiction_idx ON app.app_user (jurisdiction_id);
CREATE INDEX app_user_active_idx       ON app.app_user (is_active) WHERE is_active;

CREATE TRIGGER app_user_touch BEFORE UPDATE ON app.app_user
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();

CREATE TABLE app.user_role (
    user_id     bigint NOT NULL REFERENCES app.app_user(id) ON DELETE CASCADE,
    role_id     bigint NOT NULL REFERENCES app.role(id) ON DELETE CASCADE,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    granted_by  bigint REFERENCES app.app_user(id),
    PRIMARY KEY (user_id, role_id)
);

-- Refresh tokens are stored as hashes so a database read cannot be replayed as
-- a session. Revocation is a row update, which is why they are stored at all.
CREATE TABLE app.refresh_token (
    id          bigserial PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES app.app_user(id) ON DELETE CASCADE,
    token_hash  text NOT NULL UNIQUE,
    issued_at   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,
    issued_ip   inet,
    user_agent  text
);

CREATE INDEX refresh_token_user_idx ON app.refresh_token (user_id);
CREATE INDEX refresh_token_live_idx ON app.refresh_token (expires_at)
    WHERE revoked_at IS NULL;

-- API keys for machine onboarding (Model 1's "API-based camera onboarding").
CREATE TABLE app.api_key (
    id              bigserial PRIMARY KEY,
    label           text NOT NULL,
    key_prefix      text NOT NULL UNIQUE,
    key_hash        text NOT NULL,
    department_id   bigint REFERENCES app.department(id) ON DELETE RESTRICT,
    jurisdiction_id bigint NOT NULL REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,
    role_id         bigint NOT NULL REFERENCES app.role(id) ON DELETE RESTRICT,
    is_active       boolean NOT NULL DEFAULT true,
    expires_at      timestamptz,
    last_used_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      bigint REFERENCES app.app_user(id)
);

CREATE INDEX api_key_active_idx ON app.api_key (key_prefix) WHERE is_active;

-- 011_safety_zone.sql — curated women's-safety risk zones.
--
-- These are deliberately NOT "official crime statistics" and the schema says so:
-- `basis` defaults to 'curated' (a named operator's judgement, recorded as such)
-- rather than being presented as authoritative government crime data, which this
-- platform has no source for and must not fabricate. `alert_derived` exists as a
-- future basis once enough real SOS/women_safety_risk alert history has
-- accumulated to compute zone risk from it (see docs/DATA_HANDLING_AND_RETENTION.md
-- and docs/AUTOMATED_PROCESSING_DISCLOSURE.md, which apply the same principle to
-- watchlist matching) — the column exists now so that transition never needs a
-- schema change, only a backfill.
--
-- Same jurisdiction-scoping pattern as app.camera (002_camera.sql) and
-- app.watchlist_entry (007_watchlist.sql): a denormalised, trigger-maintained
-- `jurisdiction_path` for an index-friendly RLS prefix test, one function per
-- table rather than a shared one, by the same precedent 007 already set.

CREATE TABLE app.safety_zone (
    id                bigserial PRIMARY KEY,
    name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),

    jurisdiction_id   bigint NOT NULL REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,
    jurisdiction_path text,   -- denormalised for RLS; synced by trigger below

    center            geography(Point, 4326) NOT NULL,
    radius_m          numeric(8,2) NOT NULL CHECK (radius_m > 0 AND radius_m <= 5000),

    -- Same vocabulary as app.watchlist_entry.risk_level (007_watchlist.sql), so
    -- the console's existing risk styling/colours apply unchanged.
    risk_level        text NOT NULL DEFAULT 'medium'
                          CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    basis             text NOT NULL DEFAULT 'curated'
                          CHECK (basis IN ('curated', 'alert_derived')),
    note              text,

    is_active         boolean NOT NULL DEFAULT true,

    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        bigint REFERENCES app.app_user(id),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        bigint REFERENCES app.app_user(id)
);

CREATE INDEX safety_zone_center_idx ON app.safety_zone USING gist (center);
CREATE INDEX safety_zone_jurisdiction_path_idx
    ON app.safety_zone (jurisdiction_path text_pattern_ops);
CREATE INDEX safety_zone_active_idx ON app.safety_zone (is_active);

CREATE OR REPLACE FUNCTION app.safety_zone_sync_jurisdiction_path() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    SELECT j.path INTO NEW.jurisdiction_path FROM app.jurisdiction j WHERE j.id = NEW.jurisdiction_id;
    IF NEW.jurisdiction_path IS NULL THEN
        RAISE EXCEPTION 'jurisdiction_id % does not exist', NEW.jurisdiction_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER safety_zone_sync_path
    BEFORE INSERT OR UPDATE OF jurisdiction_id ON app.safety_zone
    FOR EACH ROW EXECUTE FUNCTION app.safety_zone_sync_jurisdiction_path();

CREATE TRIGGER safety_zone_touch BEFORE UPDATE ON app.safety_zone
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------
ALTER TABLE app.safety_zone ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.safety_zone FORCE ROW LEVEL SECURITY;

-- Read gates on alert.read, not a bare jurisdiction check: a risk zone is
-- safety-relevant information of the same sensitivity as the alerts it will
-- eventually be derived from, so whoever may see alerts for a jurisdiction may
-- see its zones, and no one else.
CREATE POLICY safety_zone_select ON app.safety_zone FOR SELECT
USING (
    app.has_permission('alert.read')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY safety_zone_insert ON app.safety_zone FOR INSERT
WITH CHECK (
    app.has_permission('safety_zone.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY safety_zone_update ON app.safety_zone FOR UPDATE
USING (
    app.has_permission('safety_zone.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
)
WITH CHECK (
    app.has_permission('safety_zone.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY safety_zone_delete ON app.safety_zone FOR DELETE
USING (
    app.has_permission('safety_zone.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);


-- ---------------------------------------------------------------------------
-- Permission + role grants
-- ---------------------------------------------------------------------------
INSERT INTO app.permission (code, description) VALUES
    ('safety_zone.write', 'Mark or edit curated women''s-safety risk zones on the map')
ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description;

-- Same tier as camera.create: state and district administrators curate zones,
-- not every operator — this is a judgement call about a real place, made by
-- someone accountable for it, not a bulk-editable dataset.
INSERT INTO app.role_permission (role_id, permission_code)
SELECT r.id, 'safety_zone.write'
FROM   app.role r
WHERE  r.code IN ('state_admin', 'district_admin')
ON CONFLICT (role_id, permission_code) DO NOTHING;

DO $$
DECLARE
    app_role text := coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app');
BEGIN
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON app.safety_zone TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.safety_zone_id_seq TO %I', app_role);
END $$;

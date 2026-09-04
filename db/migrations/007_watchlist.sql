-- 007_watchlist.sql — the searchable watchlist, its match log, alerts, and the
-- citizen/operator SOS channel. This is Step 3's "watchlist database of stolen
-- vehicles / wanted, missing persons / blacklisted vehicles / suspect watchlists"
-- plus the "automated real-time alert generation on match" requirement.
--
-- Three tables carry genuinely different lifecycles, kept separate on purpose:
--
--   watchlist_entry  — what to look for. An operator's decision, edited rarely.
--   watchlist_match  — what an edge worker actually saw. Append-only, one row
--                       per hit, the evidentiary record a "criminal mapping"
--                       query (everywhere entry X was seen, in order) reads.
--   alert            — the operational judgement built from a match (or from a
--                       women's-safety pattern, or an SOS press). Has a
--                       lifecycle (open/acknowledged/closed) a match does not:
--                       an alert can be triaged as a false positive without
--                       ever un-happening the sighting that produced it.
--
-- Matching itself happens at the edge, in the worker process — see
-- services/analytics/watchlist.py's module docstring for why. This schema is
-- the system of record an operator edits and the log those workers write to,
-- not where the comparison runs.

-- ---------------------------------------------------------------------------
-- Watchlist entries
-- ---------------------------------------------------------------------------
CREATE TABLE app.watchlist_entry (
    id              bigserial PRIMARY KEY,
    entry_type      text NOT NULL CHECK (entry_type IN (
                        'stolen_vehicle', 'blacklisted_vehicle',
                        'wanted_person', 'missing_person', 'suspect'
                    )),
    risk_level      text NOT NULL DEFAULT 'medium'
                        CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    status          text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'resolved', 'expired')),

    -- Vehicle basis. Normalised the same way services/analytics/stages/plate.py
    -- normalises a read (uppercase, no separators), so a stored entry and a live
    -- OCR read are comparable byte-for-byte without either side reformatting —
    -- see the CHECK, which mirrors app.camera_credential's stance that a
    -- validation the database can enforce is worth enforcing there too.
    plate_number    text CHECK (plate_number ~ '^[A-Z0-9]{4,15}$'),

    -- Person basis. The embedding is what an edge worker's face stage actually
    -- compares against (see services/analytics/watchlist.py); it is deliberately
    -- NOT a photo. A face photo is a much larger, much more sensitive artefact
    -- than the few dozen floats an embedding model reduces it to, and nothing
    -- here needs the photo itself to do the one job this table exists for.
    -- Storing it is a future evidence-vault concern with its own access
    -- controls, not this table's.
    person_embedding jsonb,
    embedding_model  text,               -- e.g. 'insightface:buffalo_l'. Empty
                                          -- for a plain stub-backend test entry.

    label           text NOT NULL DEFAULT '',   -- e.g. "2019 white Swift, stolen".
    case_reference  text,
    notes           text,

    department_id     bigint REFERENCES app.department(id) ON DELETE RESTRICT,
    jurisdiction_id    bigint NOT NULL REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,
    jurisdiction_path  text,              -- Denormalised for RLS; kept in sync by
                                           -- trigger below, same pattern as
                                           -- app.camera.jurisdiction_path.

    source          text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'api', 'bulk')),
    expires_at      timestamptz,

    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      bigint REFERENCES app.app_user(id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      bigint REFERENCES app.app_user(id),

    CONSTRAINT watchlist_vehicle_needs_plate CHECK (
        entry_type NOT IN ('stolen_vehicle', 'blacklisted_vehicle') OR plate_number IS NOT NULL
    ),
    CONSTRAINT watchlist_person_type_has_no_plate CHECK (
        entry_type NOT IN ('wanted_person', 'missing_person', 'suspect') OR plate_number IS NULL
    )
);

CREATE INDEX watchlist_entry_plate_idx  ON app.watchlist_entry (plate_number) WHERE status = 'active';
CREATE INDEX watchlist_entry_status_idx ON app.watchlist_entry (status);
CREATE INDEX watchlist_entry_type_idx   ON app.watchlist_entry (entry_type);
CREATE INDEX watchlist_entry_jurisdiction_path_idx
    ON app.watchlist_entry (jurisdiction_path text_pattern_ops);

CREATE OR REPLACE FUNCTION app.watchlist_entry_sync_jurisdiction_path() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    SELECT j.path INTO NEW.jurisdiction_path FROM app.jurisdiction j WHERE j.id = NEW.jurisdiction_id;
    IF NEW.jurisdiction_path IS NULL THEN
        RAISE EXCEPTION 'jurisdiction_id % does not exist', NEW.jurisdiction_id;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER watchlist_entry_sync_path
    BEFORE INSERT OR UPDATE OF jurisdiction_id ON app.watchlist_entry
    FOR EACH ROW EXECUTE FUNCTION app.watchlist_entry_sync_jurisdiction_path();

CREATE TRIGGER watchlist_entry_touch BEFORE UPDATE ON app.watchlist_entry
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Match log — append-only, one row per hit an edge worker reported
-- ---------------------------------------------------------------------------
CREATE TABLE app.watchlist_match (
    id            bigserial PRIMARY KEY,
    entry_id      bigint NOT NULL REFERENCES app.watchlist_entry(id) ON DELETE CASCADE,
    camera_id     bigint REFERENCES app.camera(id) ON DELETE SET NULL,
    track_id      text,
    basis         text NOT NULL CHECK (basis IN ('plate', 'face')),
    confidence    numeric(5,4) CHECK (confidence >= 0 AND confidence <= 1),
    raw_value     text,           -- the plate text read, or a similarity note
    matched_at    timestamptz NOT NULL DEFAULT now(),
    alert_id      bigint          -- set once the match's alert exists; see below
);

CREATE INDEX watchlist_match_entry_idx  ON app.watchlist_match (entry_id, matched_at DESC);
CREATE INDEX watchlist_match_camera_idx ON app.watchlist_match (camera_id, matched_at DESC);
CREATE INDEX watchlist_match_time_idx   ON app.watchlist_match (matched_at DESC);

COMMENT ON TABLE app.watchlist_match IS
    'One row per edge-worker hit. Two matches on the same entry from two cameras '
    'are, by construction, two points on the trail a "where has X been seen" '
    'query reads — see services/analytics/rules.py''s module docstring.';


-- ---------------------------------------------------------------------------
-- Alerts — the operational judgement, with a lifecycle
-- ---------------------------------------------------------------------------
CREATE TABLE app.alert (
    id                bigserial PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN (
                          'watchlist_match_vehicle', 'watchlist_match_person',
                          'women_safety_risk', 'abandoned_object', 'crowd_surge',
                          'speed_violation', 'sos', 'stream_gap_prolonged'
                      )),
    severity          text NOT NULL CHECK (severity IN ('info', 'advisory', 'urgent', 'critical')),
    status            text NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'acknowledged', 'closed', 'false_positive')),

    camera_id         bigint REFERENCES app.camera(id) ON DELETE SET NULL,
    department_id     bigint REFERENCES app.department(id) ON DELETE SET NULL,
    jurisdiction_id   bigint REFERENCES app.jurisdiction(id) ON DELETE SET NULL,
    jurisdiction_path text,        -- Denormalised for RLS. NULL means statewide-
                                    -- only visibility (e.g. an SOS with no fixed
                                    -- jurisdiction yet), matched by has_permission
                                    -- ('alert.read') rather than by subtree.

    -- Idempotency key from services.common.alerts.Alert.alert_key. A retried
    -- delivery (the sink's own retry-with-backoff, see sink.AlertSink) upserts
    -- rather than duplicating a page an operator has already seen.
    alert_key         text NOT NULL,

    track_ids         text[] NOT NULL DEFAULT '{}',
    source_event_kinds text[] NOT NULL DEFAULT '{}',
    summary           text NOT NULL,
    detail            jsonb NOT NULL DEFAULT '{}'::jsonb,
    case_reference    text,

    opened_at         timestamptz NOT NULL DEFAULT now(),
    acknowledged_at   timestamptz,
    acknowledged_by   bigint REFERENCES app.app_user(id),
    closed_at         timestamptz,
    closed_by         bigint REFERENCES app.app_user(id),
    close_reason      text,

    CONSTRAINT alert_ack_consistent CHECK (
        (acknowledged_at IS NULL) = (acknowledged_by IS NULL)
    ),
    CONSTRAINT alert_close_consistent CHECK (
        (closed_at IS NULL) = (closed_by IS NULL)
    )
);

-- One open alert per idempotency key. A partial unique index rather than a
-- table-wide one: the same watchlist entry legitimately re-alerts after an
-- earlier sighting of it was closed, and a bare UNIQUE(alert_key) would refuse
-- that second, entirely valid page.
CREATE UNIQUE INDEX alert_open_key_idx ON app.alert (alert_key) WHERE status IN ('open', 'acknowledged');

CREATE INDEX alert_status_idx        ON app.alert (status, opened_at DESC);
CREATE INDEX alert_severity_idx      ON app.alert (severity, opened_at DESC);
CREATE INDEX alert_camera_idx        ON app.alert (camera_id, opened_at DESC);
CREATE INDEX alert_kind_idx          ON app.alert (kind, opened_at DESC);
CREATE INDEX alert_jurisdiction_path_idx ON app.alert (jurisdiction_path text_pattern_ops);

ALTER TABLE app.watchlist_match
    ADD CONSTRAINT watchlist_match_alert_fk
    FOREIGN KEY (alert_id) REFERENCES app.alert(id) ON DELETE SET NULL;


-- ---------------------------------------------------------------------------
-- Women's-safety SOS — a citizen/operator/kiosk panic signal, no video required
-- ---------------------------------------------------------------------------
-- Deliberately a separate table from app.alert rather than just another `kind`:
-- an SOS carries its own reporter-facing fields (channel, free-text location
-- when no camera is nearby) that no video-derived alert has, and keeping it
-- separate means a schema change here can never touch the video-analytics path.
-- Every SOS still produces exactly one app.alert row (kind='sos') through the
-- same operator queue as everything else — see api/routers/alerts.py — so
-- "video-derived" and "citizen-reported" signals are triaged in one place.
CREATE TABLE app.women_safety_sos (
    id              bigserial PRIMARY KEY,
    channel         text NOT NULL CHECK (channel IN ('kiosk', 'mobile_app', 'operator', 'helpline')),
    camera_id       bigint REFERENCES app.camera(id) ON DELETE SET NULL,
    jurisdiction_id bigint REFERENCES app.jurisdiction(id) ON DELETE SET NULL,
    location        geography(Point, 4326),
    reported_at     timestamptz NOT NULL DEFAULT now(),
    notes           text,
    alert_id        bigint REFERENCES app.alert(id) ON DELETE SET NULL,
    resolved_at     timestamptz,
    resolved_by     bigint REFERENCES app.app_user(id)
);

CREATE INDEX sos_reported_idx ON app.women_safety_sos (reported_at DESC);
CREATE INDEX sos_location_idx ON app.women_safety_sos USING gist (location);


-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------
-- Reuses app.path_in_scope / app.is_statewide / app.has_permission from
-- 004_rls.sql rather than redefining subtree containment, for the reason that
-- migration states: one predicate, not one per table, to drift out of sync.

ALTER TABLE app.watchlist_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.watchlist_entry FORCE ROW LEVEL SECURITY;

CREATE POLICY watchlist_entry_select ON app.watchlist_entry FOR SELECT
USING (
    app.has_permission('watchlist.read')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY watchlist_entry_insert ON app.watchlist_entry FOR INSERT
WITH CHECK (
    app.has_permission('watchlist.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

CREATE POLICY watchlist_entry_update ON app.watchlist_entry FOR UPDATE
USING (
    app.has_permission('watchlist.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
)
WITH CHECK (
    app.has_permission('watchlist.write')
    AND (app.is_statewide() OR app.path_in_scope(jurisdiction_path))
);

-- No DELETE policy: an entry is resolved or expired (status), never removed —
-- the match log references it, and deleting it would orphan evidence.

ALTER TABLE app.watchlist_match ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.watchlist_match FORCE ROW LEVEL SECURITY;

-- Composes off watchlist_entry visibility, the same way camera_health_check
-- composes off camera visibility in 004_rls.sql.
CREATE POLICY watchlist_match_select ON app.watchlist_match FOR SELECT
USING (EXISTS (SELECT 1 FROM app.watchlist_entry e WHERE e.id = entry_id));

-- Written by the analytics ingestion endpoint on behalf of a machine API key,
-- not by an interactive user; see api/deps.py's ingest principal. Any caller
-- who can authenticate as that principal may insert.
CREATE POLICY watchlist_match_insert ON app.watchlist_match FOR INSERT
WITH CHECK (app.has_permission('watchlist.read') OR app.has_permission('camera.health_write'));

ALTER TABLE app.alert ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.alert FORCE ROW LEVEL SECURITY;

CREATE POLICY alert_select ON app.alert FOR SELECT
USING (
    app.has_permission('alert.read')
    AND (
        app.is_statewide()
        OR jurisdiction_path IS NULL
        OR app.path_in_scope(jurisdiction_path)
    )
);

CREATE POLICY alert_insert ON app.alert FOR INSERT
WITH CHECK (app.has_permission('watchlist.read') OR app.has_permission('camera.health_write'));

CREATE POLICY alert_update ON app.alert FOR UPDATE
USING (
    app.has_permission('alert.acknowledge')
    AND (app.is_statewide() OR jurisdiction_path IS NULL OR app.path_in_scope(jurisdiction_path))
)
WITH CHECK (
    app.has_permission('alert.acknowledge')
    AND (app.is_statewide() OR jurisdiction_path IS NULL OR app.path_in_scope(jurisdiction_path))
);

ALTER TABLE app.women_safety_sos ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.women_safety_sos FORCE ROW LEVEL SECURITY;

CREATE POLICY sos_select ON app.women_safety_sos FOR SELECT
USING (app.has_permission('alert.read') AND (app.is_statewide() OR jurisdiction_id IS NULL
    OR EXISTS (SELECT 1 FROM app.jurisdiction j WHERE j.id = jurisdiction_id AND app.path_in_scope(j.path))));

-- Insert has no permission gate: an SOS is, by design, reachable from a public
-- kiosk or an unauthenticated mobile flow (see api/routers/alerts.py's
-- module docstring on why POST /api/sos is not behind a login) — a panic button
-- that requires a police login first has failed at the one moment it matters.
CREATE POLICY sos_insert ON app.women_safety_sos FOR INSERT WITH CHECK (true);

CREATE POLICY sos_update ON app.women_safety_sos FOR UPDATE
USING (app.has_permission('alert.acknowledge'))
WITH CHECK (app.has_permission('alert.acknowledge'));


-- ---------------------------------------------------------------------------
-- Grants
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    app_role text := coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app');
BEGIN
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.watchlist_entry TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.watchlist_match TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.alert TO %I', app_role);
    EXECUTE format('GRANT SELECT, INSERT, UPDATE ON app.women_safety_sos TO %I', app_role);

    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.watchlist_entry_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.watchlist_match_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.alert_id_seq TO %I', app_role);
    EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE app.women_safety_sos_id_seq TO %I', app_role);
END $$;

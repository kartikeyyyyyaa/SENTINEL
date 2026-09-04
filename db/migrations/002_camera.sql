-- 002_camera.sql — the camera asset registry, credentials vault, and health history.
--
-- Model 1 is explicitly a *metadata and asset visibility* layer: it stores where
-- cameras are, who owns them, and whether they are alive. It does not centralise
-- video. The stream URLs recorded here are handed to the Model 2 viewer and the
-- Model 3 federation layer; nothing in this migration streams anything.

CREATE TABLE app.camera (
    id                bigserial PRIMARY KEY,
    -- Human-facing asset code, e.g. 'AHM-NAV-0142'. Stable across re-imports.
    code              text NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9][A-Z0-9-]{2,63}$'),
    name              text NOT NULL,

    department_id     bigint NOT NULL REFERENCES app.department(id) ON DELETE RESTRICT,
    jurisdiction_id   bigint NOT NULL REFERENCES app.jurisdiction(id) ON DELETE RESTRICT,

    camera_type       text NOT NULL CHECK (camera_type IN (
                          'fixed', 'ptz', 'dome', 'bullet', 'anpr',
                          'thermal', 'panoramic', 'body_worn', 'mobile'
                      )),
    make              text,
    model             text,
    serial_no         text,
    firmware_version  text,

    ip_address        inet,
    mac_address       macaddr,

    -- Stream characteristics as *declared* by the vendor or catalogue. The
    -- integration guide warns these are unreliable; they are inventory metadata,
    -- never an input to timing logic.
    codec             text CHECK (codec IN ('h264', 'h265', 'mjpeg', 'av1', 'mpeg4')),
    resolution_w      integer CHECK (resolution_w > 0),
    resolution_h      integer CHECK (resolution_h > 0),
    declared_fps      numeric(5,2) CHECK (declared_fps > 0),
    bitrate_kbps      integer CHECK (bitrate_kbps > 0),

    -- Geometry. bearing/fov/range together define the coverage wedge used by
    -- gap analysis. A camera with NULL bearing contributes only a point.
    location          geography(Point, 4326) NOT NULL,
    address           text,
    landmark          text,
    bearing_deg       numeric(6,2) CHECK (bearing_deg >= 0 AND bearing_deg < 360),
    fov_deg           numeric(6,2) CHECK (fov_deg > 0 AND fov_deg <= 360),
    range_m           numeric(8,2) CHECK (range_m > 0 AND range_m <= 2000),
    height_m          numeric(5,2) CHECK (height_m >= 0),

    status            text NOT NULL DEFAULT 'active' CHECK (status IN (
                          'active', 'inactive', 'faulty',
                          'maintenance', 'decommissioned', 'planned'
                      )),
    connectivity      text CHECK (connectivity IN (
                          'fiber', 'lan', 'wireless', '4g', '5g', 'vsat', 'unknown'
                      )),
    power_backup      text CHECK (power_backup IN ('none', 'ups', 'solar', 'generator', 'unknown')),

    storage_type      text CHECK (storage_type IN ('edge_sd', 'local_nvr', 'site_server', 'central', 'none')),
    retention_days    integer CHECK (retention_days >= 0),

    -- Lifecycle dates, which drive the ageing-infrastructure report.
    install_date      date,
    commission_date   date,
    warranty_expiry   date,
    last_service_date date,

    -- Access paths. Populated for cameras reachable directly (Model 2) or via a
    -- departmental VMS (Model 3).
    rtsp_url          text,
    hls_url           text,
    whep_url          text,
    onvif_url         text,
    vms_platform      text,
    vms_camera_id     text,

    -- Provenance: which onboarding path created this row.
    source            text NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'bulk', 'api', 'sync')),
    external_id       text,
    import_batch_id   bigint,

    is_public_view    boolean NOT NULL DEFAULT false,
    notes             text,

    created_at        timestamptz NOT NULL DEFAULT now(),
    created_by        bigint REFERENCES app.app_user(id),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    updated_by        bigint REFERENCES app.app_user(id),

    -- A wedge needs both a direction and a spread; reject half-specified geometry
    -- rather than silently drawing a nonsense coverage polygon.
    CONSTRAINT camera_wedge_complete CHECK (
        (bearing_deg IS NULL AND fov_deg IS NULL) OR
        (bearing_deg IS NOT NULL AND fov_deg IS NOT NULL AND range_m IS NOT NULL)
    )
);

CREATE INDEX camera_location_idx     ON app.camera USING gist (location);
CREATE INDEX camera_department_idx   ON app.camera (department_id);
CREATE INDEX camera_jurisdiction_idx ON app.camera (jurisdiction_id);
CREATE INDEX camera_status_idx       ON app.camera (status);
CREATE INDEX camera_type_idx         ON app.camera (camera_type);
CREATE INDEX camera_name_trgm_idx    ON app.camera USING gin (name gin_trgm_ops);
CREATE UNIQUE INDEX camera_external_id_idx ON app.camera (source, external_id)
    WHERE external_id IS NOT NULL;

CREATE TRIGGER camera_touch BEFORE UPDATE ON app.camera
    FOR EACH ROW EXECUTE FUNCTION app.touch_updated_at();


-- ---------------------------------------------------------------------------
-- Credential vault
-- ---------------------------------------------------------------------------
-- Camera passwords are the crown jewels of a CCTV platform: they grant live
-- access to the physical world. They live in a separate table so that the
-- ordinary camera-read path never touches them, they are encrypted with a
-- per-camera data key wrapped by a master key held outside the database, and no
-- API response is permitted to return them.
CREATE TABLE app.camera_credential (
    camera_id       bigint PRIMARY KEY REFERENCES app.camera(id) ON DELETE CASCADE,
    username_enc    bytea NOT NULL,
    password_enc    bytea NOT NULL,
    wrapped_dek     bytea NOT NULL,
    key_version     integer NOT NULL DEFAULT 1,
    rotated_at      timestamptz NOT NULL DEFAULT now(),
    rotated_by      bigint REFERENCES app.app_user(id),
    -- Set when a probe finds the camera still using a vendor default password.
    is_default_credential boolean NOT NULL DEFAULT false
);

COMMENT ON TABLE app.camera_credential IS
    'Encrypted camera credentials. Envelope encryption: AES-256-GCM data key per '
    'camera, wrapped by CREDENTIAL_MASTER_KEY. Never exposed through the API.';


-- ---------------------------------------------------------------------------
-- Health history
-- ---------------------------------------------------------------------------
-- Liveness is established WITHOUT consuming media: a TCP connect, an RTSP
-- OPTIONS/DESCRIBE handshake that stops before SETUP, or an ONVIF
-- GetSystemDateAndTime call. This respects Model 1's boundary (no centralised
-- streaming) while still answering "is this camera alive right now".
CREATE TABLE app.camera_health_check (
    id          bigserial PRIMARY KEY,
    camera_id   bigint NOT NULL REFERENCES app.camera(id) ON DELETE CASCADE,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    probe       text NOT NULL CHECK (probe IN ('tcp', 'rtsp_options', 'rtsp_describe', 'onvif', 'http')),
    is_live     boolean NOT NULL,
    latency_ms  integer CHECK (latency_ms >= 0),
    error_code  text,
    detail      text
);

CREATE INDEX camera_health_recent_idx ON app.camera_health_check (camera_id, checked_at DESC);
CREATE INDEX camera_health_time_idx   ON app.camera_health_check (checked_at DESC);

-- Latest probe per camera, for the map's status layer. DISTINCT ON is the
-- cheapest correct way to express "most recent row per group" in Postgres.
CREATE VIEW app.camera_health_current AS
SELECT DISTINCT ON (camera_id)
       camera_id, checked_at, probe, is_live, latency_ms, error_code
FROM   app.camera_health_check
ORDER  BY camera_id, checked_at DESC;


-- ---------------------------------------------------------------------------
-- Bulk import batches
-- ---------------------------------------------------------------------------
CREATE TABLE app.import_batch (
    id            bigserial PRIMARY KEY,
    filename      text NOT NULL,
    content_hash  text NOT NULL,
    uploaded_by   bigint REFERENCES app.app_user(id),
    uploaded_at   timestamptz NOT NULL DEFAULT now(),
    is_dry_run    boolean NOT NULL DEFAULT false,
    rows_total    integer NOT NULL DEFAULT 0,
    rows_inserted integer NOT NULL DEFAULT 0,
    rows_updated  integer NOT NULL DEFAULT 0,
    rows_rejected integer NOT NULL DEFAULT 0,
    report        jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX import_batch_uploaded_idx ON app.import_batch (uploaded_at DESC);

ALTER TABLE app.camera
    ADD CONSTRAINT camera_import_batch_fk
    FOREIGN KEY (import_batch_id) REFERENCES app.import_batch(id) ON DELETE SET NULL;

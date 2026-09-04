-- 003_audit.sql — append-only, hash-chained audit trail.
--
-- Design intent: make the audit trail trustworthy even if the application is
-- fully compromised.
--
--   1. The chain is computed by a database TRIGGER, not by the application. An
--      attacker with application-level code execution still cannot insert a row
--      whose hash does not chain to its predecessor.
--   2. UPDATE and DELETE are blocked by trigger *and* by revoked privileges, so
--      history cannot be rewritten through the normal connection.
--   3. Any tampering performed out-of-band (direct superuser access, disk edit)
--      breaks the chain and is located precisely by app.verify_audit_chain().
--
-- The hash payload format is mirrored byte-for-byte in
-- services/registry/app/core/audit.py so the verifier can be run independently
-- of the database. Changing one without the other breaks verification, which is
-- exactly the kind of mistake the unit tests exist to catch.

CREATE TABLE app.audit_log (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),

    actor_user_id  bigint REFERENCES app.app_user(id) ON DELETE SET NULL,
    actor_username text,
    actor_ip       inet,
    actor_agent    text,

    action         text NOT NULL,
    resource_type  text,
    resource_id    text,

    -- Why the data was accessed. Purpose limitation is a DPDP Act 2023
    -- expectation and turns "who looked at this camera" into "who looked at this
    -- camera, and under which case number".
    purpose        text,
    case_reference text,

    outcome        text NOT NULL DEFAULT 'success'
                       CHECK (outcome IN ('success', 'denied', 'error')),

    -- Canonical JSON as text, not jsonb: the hash must be reproducible outside
    -- Postgres, and jsonb's key ordering is an implementation detail we would be
    -- depending on. Query it with detail::jsonb when needed.
    detail         text,

    prev_hash      text,
    row_hash       text NOT NULL
);

CREATE INDEX audit_log_at_idx        ON app.audit_log (at DESC);
CREATE INDEX audit_log_actor_idx     ON app.audit_log (actor_user_id, at DESC);
CREATE INDEX audit_log_action_idx    ON app.audit_log (action, at DESC);
CREATE INDEX audit_log_resource_idx  ON app.audit_log (resource_type, resource_id, at DESC);
CREATE INDEX audit_log_outcome_idx   ON app.audit_log (outcome, at DESC) WHERE outcome <> 'success';


-- Chain construction is serialised by a fixed advisory lock (918273645) so two
-- concurrent inserts cannot both read the same predecessor and fork the chain.
CREATE OR REPLACE FUNCTION app.audit_chain_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    prev    text;
    payload text;
BEGIN
    PERFORM pg_advisory_xact_lock(918273645);

    SELECT row_hash INTO prev
    FROM   app.audit_log
    ORDER  BY id DESC
    LIMIT  1;

    NEW.prev_hash := prev;
    NEW.at := COALESCE(NEW.at, now());

    payload :=
        COALESCE(prev, 'GENESIS')                                              || '|' ||
        NEW.id::text                                                           || '|' ||
        to_char(NEW.at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')         || '|' ||
        COALESCE(NEW.actor_user_id::text, '')                                  || '|' ||
        COALESCE(NEW.actor_username, '')                                       || '|' ||
        COALESCE(host(NEW.actor_ip), '')                                       || '|' ||
        NEW.action                                                             || '|' ||
        COALESCE(NEW.resource_type, '')                                        || '|' ||
        COALESCE(NEW.resource_id, '')                                          || '|' ||
        COALESCE(NEW.purpose, '')                                              || '|' ||
        COALESCE(NEW.case_reference, '')                                       || '|' ||
        NEW.outcome                                                            || '|' ||
        COALESCE(NEW.detail, '');

    NEW.row_hash := encode(digest(payload, 'sha256'), 'hex');
    RETURN NEW;
END $$;

CREATE TRIGGER audit_log_chain
    BEFORE INSERT ON app.audit_log
    FOR EACH ROW EXECUTE FUNCTION app.audit_chain_insert();


-- Append-only enforcement.
CREATE OR REPLACE FUNCTION app.audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'app.audit_log is append-only; % is not permitted', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON app.audit_log
    FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON app.audit_log
    FOR EACH ROW EXECUTE FUNCTION app.audit_immutable();

-- TRUNCATE bypasses row-level triggers, so it needs its own statement-level guard.
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON app.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION app.audit_immutable();


-- Chain verification. Recomputes every row's hash from its stored fields and its
-- predecessor, and returns the first divergence. Exposed through the API so the
-- integrity of the trail is a demonstrable property, not a claim on a slide.
CREATE OR REPLACE FUNCTION app.verify_audit_chain(
    from_id bigint DEFAULT 0,
    max_rows integer DEFAULT 1000000
)
RETURNS TABLE (
    checked_rows   bigint,
    is_intact      boolean,
    broken_at_id   bigint,
    reason         text
)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    r          record;
    expect     text;
    payload    text;
    prev       text := NULL;
    n          bigint := 0;
    first_row  boolean := true;
BEGIN
    FOR r IN
        SELECT * FROM app.audit_log
        WHERE id > from_id
        ORDER BY id
        LIMIT max_rows
    LOOP
        -- When starting mid-chain, adopt the stored prev_hash as the baseline.
        IF first_row THEN
            prev := r.prev_hash;
            first_row := false;
        END IF;

        IF COALESCE(r.prev_hash, '') <> COALESCE(prev, '') THEN
            checked_rows := n; is_intact := false; broken_at_id := r.id;
            reason := 'prev_hash does not match the preceding row''s row_hash';
            RETURN NEXT; RETURN;
        END IF;

        payload :=
            COALESCE(prev, 'GENESIS')                                          || '|' ||
            r.id::text                                                         || '|' ||
            to_char(r.at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')       || '|' ||
            COALESCE(r.actor_user_id::text, '')                                || '|' ||
            COALESCE(r.actor_username, '')                                     || '|' ||
            COALESCE(host(r.actor_ip), '')                                     || '|' ||
            r.action                                                           || '|' ||
            COALESCE(r.resource_type, '')                                      || '|' ||
            COALESCE(r.resource_id, '')                                        || '|' ||
            COALESCE(r.purpose, '')                                            || '|' ||
            COALESCE(r.case_reference, '')                                     || '|' ||
            r.outcome                                                          || '|' ||
            COALESCE(r.detail, '');

        expect := encode(digest(payload, 'sha256'), 'hex');

        IF expect <> r.row_hash THEN
            checked_rows := n; is_intact := false; broken_at_id := r.id;
            reason := 'row_hash does not match recomputed hash; row contents were altered';
            RETURN NEXT; RETURN;
        END IF;

        prev := r.row_hash;
        n := n + 1;
    END LOOP;

    checked_rows := n; is_intact := true; broken_at_id := NULL; reason := NULL;
    RETURN NEXT;
END $$;

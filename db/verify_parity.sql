-- verify_parity.sql — proves the SQL and Python audit hashes agree.
--
-- Run against a migrated database:
--
--     docker compose exec -T db psql -U postgres -d sentinel -v ON_ERROR_STOP=1 \
--         -f /srv/db/verify_parity.sql
--
-- The constant below is the same one pinned in
-- services/registry/tests/test_audit.py::PayloadFormat::test_known_vector.
-- Three things must agree: the SQL trigger, the Python mirror, and this literal.
-- Editing any one of them without the others makes a test fail — which is the
-- point. An independent verifier is worthless if it can drift from the thing it
-- verifies without anyone noticing.

\set ON_ERROR_STOP on

DO $$
DECLARE
    payload  text;
    got      text;
    expected text := '19ff06c0299c6fc7cc8d930d67ef45636ba2733a1b31cbe7022f718dd60c3d59';
BEGIN
    -- Built exactly as app.audit_chain_insert() builds it, from the same inputs
    -- the Python test uses.
    payload :=
        'GENESIS'                                                                  || '|' ||
        1::text                                                                    || '|' ||
        to_char('2026-09-07 04:05:06+00'::timestamptz AT TIME ZONE 'UTC',
                'YYYY-MM-DD HH24:MI:SS.US')                                        || '|' ||
        1::text                                                                    || '|' ||
        'state.admin'                                                              || '|' ||
        host('127.0.0.1'::inet)                                                    || '|' ||
        'auth.login.success'                                                       || '|' ||
        ''  || '|' || ''  || '|' || ''  || '|' || ''                               || '|' ||
        'success'                                                                  || '|' ||
        '';

    IF payload <> 'GENESIS|1|2026-09-07 04:05:06.000000|1|state.admin|127.0.0.1|auth.login.success|||||success|' THEN
        RAISE EXCEPTION E'payload format diverged from the Python mirror.\ngot:      %\nexpected: %',
            payload,
            'GENESIS|1|2026-09-07 04:05:06.000000|1|state.admin|127.0.0.1|auth.login.success|||||success|';
    END IF;

    got := encode(digest(payload, 'sha256'), 'hex');
    IF got <> expected THEN
        RAISE EXCEPTION 'hash mismatch: got % expected %', got, expected;
    END IF;

    RAISE NOTICE 'payload format and SHA-256 match the Python mirror';
END $$;


-- ---------------------------------------------------------------------------
-- Live trigger behaviour, verified end to end and then rolled back.
-- ---------------------------------------------------------------------------
BEGIN;

-- Three rows through the real trigger.
INSERT INTO app.audit_log (action, actor_username, actor_ip, outcome)
VALUES ('parity.test.one',   'parity', '127.0.0.1', 'success'),
       ('parity.test.two',   'parity', '127.0.0.1', 'success'),
       ('parity.test.three', 'parity', '127.0.0.1', 'denied');

DO $$
DECLARE
    v record;
    n bigint;
BEGIN
    SELECT count(*) INTO n FROM app.audit_log WHERE actor_username = 'parity';
    IF n <> 3 THEN
        RAISE EXCEPTION 'expected 3 inserted rows, found %', n;
    END IF;

    -- Every row must have been hashed and chained by the trigger.
    IF EXISTS (SELECT 1 FROM app.audit_log WHERE row_hash IS NULL) THEN
        RAISE EXCEPTION 'a row has no row_hash: the trigger did not fire';
    END IF;

    IF EXISTS (SELECT 1 FROM app.audit_log WHERE length(row_hash) <> 64) THEN
        RAISE EXCEPTION 'row_hash is not a 64-character hex digest';
    END IF;

    -- The first row of a fresh log chains from GENESIS, i.e. has no predecessor.
    -- Later rows must each point at the previous row's hash.
    IF EXISTS (
        SELECT 1
        FROM   app.audit_log a
        JOIN   app.audit_log b ON b.id = (
                   SELECT max(id) FROM app.audit_log WHERE id < a.id
               )
        WHERE  a.prev_hash IS DISTINCT FROM b.row_hash
    ) THEN
        RAISE EXCEPTION 'chain linkage is broken';
    END IF;

    SELECT * INTO v FROM app.verify_audit_chain(0, 1000000);
    IF NOT v.is_intact THEN
        RAISE EXCEPTION 'verify_audit_chain says the chain is broken at id %: %',
            v.broken_at_id, v.reason;
    END IF;
    RAISE NOTICE 'trigger chained % rows; verify_audit_chain reports intact', v.checked_rows;
END $$;


-- Append-only enforcement. Each of these must fail.
DO $$
DECLARE
    target bigint;
BEGIN
    SELECT id INTO target FROM app.audit_log WHERE actor_username = 'parity' LIMIT 1;

    BEGIN
        UPDATE app.audit_log SET action = 'tampered' WHERE id = target;
        RAISE EXCEPTION 'UPDATE on app.audit_log succeeded — it must not';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'UPDATE correctly refused';
    END;

    BEGIN
        DELETE FROM app.audit_log WHERE id = target;
        RAISE EXCEPTION 'DELETE on app.audit_log succeeded — it must not';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'DELETE correctly refused';
    END;

    BEGIN
        TRUNCATE app.audit_log;
        RAISE EXCEPTION 'TRUNCATE on app.audit_log succeeded — it must not';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'TRUNCATE correctly refused';
    END;
END $$;

-- Nothing from this file is kept.
ROLLBACK;

\echo 'parity and append-only checks passed'

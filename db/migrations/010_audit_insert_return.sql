-- 010_audit_insert_return.sql — fix a silent audit-trail gap: out-of-band
-- audit rows for an actor RLS cannot yet identify were never actually being
-- written.
--
-- The bug, found by testing the SOS endpoint end to end rather than by
-- inspection: `core/audit.py`'s `record()` / `record_out_of_band()` both do
-- `INSERT INTO app.audit_log (...) RETURNING id, at, prev_hash, row_hash`.
-- Postgres RLS requires a row to satisfy the table's SELECT policy before it
-- can appear in an INSERT's RETURNING list — not merely the INSERT policy's
-- WITH CHECK (which `audit_insert`, `WITH CHECK (true)`, always satisfies).
-- `audit_select`'s USING clause is
-- `has_permission('audit.read') OR actor_user_id = app.current_user_id()`.
-- For every row this trail exists to catch — an anonymous SOS report, a login
-- attempt against a username that does not exist, a permission denial from a
-- caller who by definition lacks the permission being checked — there is no
-- `app.user_id` asserted at all, so `current_user_id()` is NULL, the equality
-- is NULL (not true), and the RETURNING clause raises exactly the same "new
-- row violates row-level security policy" error a WITH CHECK failure would.
-- `record_out_of_band`'s own `except Exception` (see its docstring — added
-- deliberately so a failed audit write cannot break the request it is
-- auditing) swallowed this into a warning log line, so the gap produced no
-- visible error anywhere: every one of those rows was silently never written.
--
-- The fix is narrow: a SECURITY DEFINER function performs the insert and
-- reads the row back itself, the same pattern `has_permission()` and its
-- neighbours in 004_rls.sql already use for "a trusted, narrowly-scoped
-- operation needs to see past the caller's own RLS view" — nothing about
-- `audit_select`'s general visibility rules changes, and the hash-chain
-- trigger (`app.audit_chain_insert()`) still fires exactly as before,
-- unaware this function exists. Ordinary reads of the trail are entirely
-- unaffected; only the write path's own read-back is repaired.
CREATE OR REPLACE FUNCTION app.write_audit_row(
    p_actor_user_id  bigint,
    p_actor_username text,
    p_actor_ip       inet,
    p_actor_agent    text,
    p_action         text,
    p_resource_type  text,
    p_resource_id    text,
    p_purpose        text,
    p_case_reference text,
    p_outcome        text,
    p_detail         text
) RETURNS TABLE (id bigint, at timestamptz, prev_hash text, row_hash text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, public AS $$
BEGIN
    RETURN QUERY
    INSERT INTO app.audit_log (
        actor_user_id, actor_username, actor_ip, actor_agent,
        action, resource_type, resource_id,
        purpose, case_reference, outcome, detail
    ) VALUES (
        p_actor_user_id, p_actor_username, p_actor_ip, p_actor_agent,
        p_action, p_resource_type, p_resource_id,
        p_purpose, p_case_reference, p_outcome, p_detail
    )
    RETURNING app.audit_log.id, app.audit_log.at, app.audit_log.prev_hash, app.audit_log.row_hash;
END $$;

COMMENT ON FUNCTION app.write_audit_row IS
    'Insert one audit row and read it back, bypassing the caller''s own RLS '
    'visibility for that read-back only (SECURITY DEFINER) — see this '
    'migration''s header for why a plain INSERT ... RETURNING cannot do this '
    'for an anonymous or not-yet-authorised actor. The audit_insert policy''s '
    'WITH CHECK still governs whether the insert itself is allowed; this '
    'function does not widen who may write an audit row, only who may see '
    'the one they just wrote.';

DO $$
DECLARE
    app_role text := coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app');
BEGIN
    EXECUTE format(
        'GRANT EXECUTE ON FUNCTION app.write_audit_row('
        'bigint, text, inet, text, text, text, text, text, text, text, text) TO %I',
        app_role
    );
END $$;

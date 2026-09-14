-- 006_token_rotation.sql — refresh-token families, so a stolen token is detectable.
--
-- The problem with long-lived refresh tokens: if one is exfiltrated (XSS, a
-- backup, a proxy log) the thief holds a valid session for its whole lifetime and
-- nothing in the system notices.
--
-- Rotation with reuse detection fixes that. Every refresh consumes the presented
-- token and issues a new one in the same *family*. A token can therefore be used
-- exactly once. If a token that was already consumed is presented again, exactly
-- one of two things happened: the token was stolen and both parties are now using
-- it, or a client retried after a lost response. Neither can be distinguished
-- from the other, and the safe response to both is the same — kill the entire
-- family and force a fresh login. The legitimate user logs in again; the thief
-- gets nothing and, crucially, we get an audit row saying so.
--
-- This is the OAuth 2.1 / BCP 212 recommendation for public clients, and it is
-- what turns token theft from silent and indefinite into loud and time-boxed.

ALTER TABLE app.refresh_token
    ADD COLUMN family_id      uuid NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN used_at        timestamptz,
    ADD COLUMN replaced_by    bigint REFERENCES app.refresh_token(id) ON DELETE SET NULL,
    ADD COLUMN revoked_reason text;

COMMENT ON COLUMN app.refresh_token.family_id IS
    'All tokens descended from one login. Revoked together on reuse detection.';
COMMENT ON COLUMN app.refresh_token.used_at IS
    'Set when the token is exchanged. A second presentation of a used token is '
    'treated as compromise of the whole family.';

CREATE INDEX refresh_token_family_idx ON app.refresh_token (family_id)
    WHERE revoked_at IS NULL;

-- Expired tokens are dead weight; the lookup path should never see them.
-- Named distinctly from 001_core.sql's refresh_token_live_idx (indexed on
-- expires_at, for a different query shape) — the two coexist rather than one
-- shadowing the other, which is what a duplicate CREATE INDEX name would do the
-- moment this migration was applied on top of 001.
CREATE INDEX refresh_token_unused_idx ON app.refresh_token (token_hash)
    WHERE revoked_at IS NULL AND used_at IS NULL;


-- Consume a token and revoke its family if it was already used.
--
-- Written as a single SQL function rather than a read-then-write in Python so the
-- check and the consume cannot be interleaved by a concurrent request. Two
-- simultaneous refreshes with the same token must produce one success and one
-- reuse alarm, never two successes — the FOR UPDATE row lock is what guarantees
-- that, and it only works if both happen in one statement.
CREATE OR REPLACE FUNCTION app.consume_refresh_token(p_token_hash text)
RETURNS TABLE (
    status     text,      -- 'ok' | 'reused' | 'expired' | 'revoked' | 'unknown'
    user_id    bigint,
    token_id   bigint,
    family_id  uuid
)
LANGUAGE plpgsql AS $$
DECLARE
    t record;
BEGIN
    SELECT * INTO t
    FROM   app.refresh_token
    WHERE  token_hash = p_token_hash
    FOR    UPDATE;

    IF NOT FOUND THEN
        RETURN QUERY SELECT 'unknown'::text, NULL::bigint, NULL::bigint, NULL::uuid;
        RETURN;
    END IF;

    -- Already consumed: treat as compromise and burn the family down.
    IF t.used_at IS NOT NULL THEN
        UPDATE app.refresh_token
           SET revoked_at = now(),
               revoked_reason = 'family revoked: reuse of a consumed token'
         WHERE family_id = t.family_id
           AND revoked_at IS NULL;

        RETURN QUERY SELECT 'reused'::text, t.user_id, t.id, t.family_id;
        RETURN;
    END IF;

    IF t.revoked_at IS NOT NULL THEN
        RETURN QUERY SELECT 'revoked'::text, t.user_id, t.id, t.family_id;
        RETURN;
    END IF;

    IF t.expires_at <= now() THEN
        RETURN QUERY SELECT 'expired'::text, t.user_id, t.id, t.family_id;
        RETURN;
    END IF;

    UPDATE app.refresh_token SET used_at = now() WHERE id = t.id;

    RETURN QUERY SELECT 'ok'::text, t.user_id, t.id, t.family_id;
END $$;


CREATE OR REPLACE FUNCTION app.revoke_token_family(p_family_id uuid, p_reason text)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    UPDATE app.refresh_token
       SET revoked_at = now(),
           revoked_reason = p_reason
     WHERE family_id = p_family_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;


-- Logout everywhere. Used on password change, role change and deactivation:
-- a permission revocation that leaves live sessions untouched is not a
-- revocation.
CREATE OR REPLACE FUNCTION app.revoke_user_tokens(p_user_id bigint, p_reason text)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    n integer;
BEGIN
    UPDATE app.refresh_token
       SET revoked_at = now(),
           revoked_reason = p_reason
     WHERE user_id = p_user_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END $$;


DO $$
DECLARE
    app_role text := coalesce(nullif(current_setting('sentinel.app_role', true), ''), 'sentinel_app');
BEGIN
    EXECUTE format('GRANT EXECUTE ON FUNCTION app.consume_refresh_token(text) TO %I', app_role);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app.revoke_token_family(uuid, text) TO %I', app_role);
    EXECUTE format('GRANT EXECUTE ON FUNCTION app.revoke_user_tokens(bigint, text) TO %I', app_role);
END $$;

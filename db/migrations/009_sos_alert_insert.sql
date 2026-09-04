-- 009_sos_alert_insert.sql — let an anonymous SOS report create its own alert.
--
-- 007_watchlist.sql's `alert_insert` policy gates on
-- `has_permission('watchlist.read') OR has_permission('camera.health_write')`
-- — correct for a machine ingest principal, which always has one of those, but
-- wrong for `POST /api/sos`. That endpoint is deliberately unauthenticated
-- (see api/routers/alerts.py's module docstring: a panic button that requires
-- a login first has failed at the one moment it matters), so the session that
-- inserts its alert row has no app.user_id at all — and therefore neither
-- permission, no matter who is running the app. `women_safety_sos`'s own
-- `sos_insert` policy already accepts this (`WITH CHECK (true)`); `app.alert`
-- needs the same allowance, scoped narrowly to `kind = 'sos'` so nothing else
-- gets a new way to write an alert without a permission check.
DROP POLICY alert_insert ON app.alert;

CREATE POLICY alert_insert ON app.alert FOR INSERT
WITH CHECK (
    kind = 'sos'
    OR app.has_permission('watchlist.read')
    OR app.has_permission('camera.health_write')
);

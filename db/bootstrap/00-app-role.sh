#!/bin/bash
# Runs once, as the Postgres superuser, on first initialisation of the data volume.
# Creates the least-privilege role the API connects as.
#
# This role intentionally has:
#   - no SUPERUSER   (superusers bypass Row-Level Security entirely)
#   - no BYPASSRLS   (same reason)
#   - no CREATEDB / CREATEROLE
#   - no schema-level DDL rights (granted selectively by the migrations)
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${APP_DB_USER}') THEN
            CREATE ROLE ${APP_DB_USER} LOGIN PASSWORD '${APP_DB_PASSWORD}'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
        END IF;
    END
    \$\$;

    -- No implicit rights anywhere. Migrations grant exactly what is needed.
    REVOKE ALL ON DATABASE ${POSTGRES_DB} FROM PUBLIC;
    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${APP_DB_USER};
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL

echo "bootstrap: role ${APP_DB_USER} ready"

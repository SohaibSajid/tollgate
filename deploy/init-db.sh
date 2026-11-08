#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=app_password="$TOLLGATE_DB_PASSWORD" --set=ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE tollgate_app LOGIN PASSWORD :'app_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL

#!/bin/sh
set -eu

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=db_name="$POSTGRES_DB" \
  --set=app_password="$APP_DB_PASSWORD" \
  --set=worker_password="$WORKER_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE rag_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_app') \gexec

SELECT format('CREATE ROLE rag_worker LOGIN PASSWORD %L BYPASSRLS', :'worker_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rag_worker') \gexec

GRANT CONNECT ON DATABASE :"db_name" TO rag_app, rag_worker;
GRANT USAGE ON SCHEMA public TO rag_app, rag_worker;

-- Future grants: Alembic migrations (0001-0008) assign table-level privileges via
-- GRANT … TO rag_app / rag_worker. Migration 0009 revokes UPDATE+DELETE on
-- audit_logs from rag_app for append-only immutability (item 17).
SQL

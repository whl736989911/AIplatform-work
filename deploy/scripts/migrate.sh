#!/bin/sh
# =============================================================================
# Schema migration job — MUST succeed before the app container may start.
#
# Contract (see deploy/compose.production.yml):
#   * runs as the non-root application uid, read-only rootfs, no capabilities;
#   * waits (bounded) for PostgreSQL, then applies NNN_*.pg.sql migrations
#     through the supported `octop admin overview` surface, which opens the
#     control-plane database and calls run_migrations() before reading anything;
#   * afterwards re-opens the database directly and asserts, fail closed:
#       - the backend really is PostgreSQL (never the SQLite fallback),
#       - server_version matches the version pinned for production,
#       - the pgvector extension is installed (WorkBuddy vectors),
#       - _schema_version is present and readable,
#       - row-level security is enabled and forced on every RLS table;
#   * emits one machine-readable JSON line with the evidence.
#
# Exit codes: 0 migrated+verified, 1 dependency/migration failure (fail closed).
# =============================================================================
set -eu

fail() {
    printf '{"status":"failed","service":"migrate","code":"%s","message":"%s","details":{%s}}\n' \
        "$1" "$2" "$3" >&2
    exit 1
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\r\n'
}

POSTGRES_HOST="${POSTGRES_HOST:?POSTGRES_HOST is required}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:?POSTGRES_DB is required}"
POSTGRES_USER="${POSTGRES_USER:?POSTGRES_USER is required}"
WAIT_SECONDS="${OCTOP_MIGRATION_WAIT_SECONDS:-120}"
EXPECTED_SERVER_VERSION="${OCTOP_EXPECTED_DATABASE_SERVER_VERSION:-}"

password_file="${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}"
[ -f "$password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "PostgreSQL password secret file is missing" "\"path\":\"${password_file}\""
[ -r "$password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "PostgreSQL password secret file is not readable" "\"path\":\"${password_file}\""
postgres_password=$(tr -d '\r\n' <"$password_file")
[ -n "$postgres_password" ] || fail DEPENDENCY_UNAVAILABLE \
    "PostgreSQL password secret file is empty" "\"path\":\"${password_file}\""

OCTOP_DATABASE_DRIVER=postgresql
OCTOP_DATABASE_URL=$(OCTOP_DSN_USER="$POSTGRES_USER" \
    OCTOP_DSN_PASSWORD="$postgres_password" \
    OCTOP_DSN_HOST="$POSTGRES_HOST" \
    OCTOP_DSN_PORT="$POSTGRES_PORT" \
    OCTOP_DSN_DATABASE="$POSTGRES_DB" \
    python3 -c 'import os, urllib.parse as u
q = u.quote
print("postgresql://%s:%s@%s:%s/%s" % (
    q(os.environ["OCTOP_DSN_USER"], safe=""),
    q(os.environ["OCTOP_DSN_PASSWORD"], safe=""),
    os.environ["OCTOP_DSN_HOST"],
    os.environ["OCTOP_DSN_PORT"],
    q(os.environ["OCTOP_DSN_DATABASE"], safe=""),
))')
DATABASE_URL="$OCTOP_DATABASE_URL"
export OCTOP_DATABASE_DRIVER OCTOP_DATABASE_URL DATABASE_URL
unset postgres_password

# --- wait for the server, bounded, then stop (never migrate a raced database) --
waited=0
while :; do
    if DATABASE_URL="$DATABASE_URL" python3 -c '
import os, sys, psycopg
try:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
except Exception:
    sys.exit(1)
' 2>/dev/null; then
        break
    fi
    waited=$((waited + 2))
    if [ "$waited" -ge "$WAIT_SECONDS" ]; then
        fail DEPENDENCY_UNAVAILABLE \
            "PostgreSQL did not accept connections before the migration deadline" \
            "\"host\":\"${POSTGRES_HOST}\",\"port\":\"${POSTGRES_PORT}\",\"waited_seconds\":\"${waited}\""
    fi
    sleep 2
done

# --- apply migrations through the supported CLI surface -----------------------
if ! migration_output=$(octop admin overview 2>/tmp/migrate-cli.err); then
    fail DEPENDENCY_UNAVAILABLE "octop migration run failed" \
        "\"target\":\"${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}\""
fi
if ! printf '%s' "$migration_output" | python3 -c 'import json, sys; json.load(sys.stdin)' 2>/dev/null; then
    fail DEPENDENCY_UNAVAILABLE "migration command returned non-JSON output" '"command":"octop admin overview"'
fi

# --- verify the result directly, fail closed on any mismatch ------------------
verification=$(DATABASE_URL="$DATABASE_URL" \
    EXPECTED_SERVER_VERSION="$EXPECTED_SERVER_VERSION" \
    python3 - <<'PY'
import json
import os
import sys

import psycopg

expected = os.environ.get("EXPECTED_SERVER_VERSION", "").strip()
try:
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('server_version'), current_database()")
            row = cursor.fetchone()
            server_version, database = str(row[0]), str(row[1])
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vector = cursor.fetchone()
            cursor.execute("SELECT version FROM _schema_version LIMIT 1")
            schema_row = cursor.fetchone()
            schema_version = int(schema_row[0]) if schema_row else None
            # Tenant-scoped tables are identified by contract, not by name: any
            # application table exposing a tenant_id column must enforce RLS and
            # FORCE it, and that column must be a UUID.
            cursor.execute(
                """
                SELECT c.relname,
                       c.relrowsecurity,
                       c.relforcerowsecurity,
                       format_type(a.atttypid, a.atttypmod)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
                WHERE c.relkind = 'r'
                  AND NOT a.attisdropped
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY c.relname
                """
            )
            tenant_rows = cursor.fetchall()
except Exception as exc:  # dependency boundary: report the class, never the DSN
    print(json.dumps({"status": "failed", "code": "DEPENDENCY_UNAVAILABLE",
                      "error": type(exc).__name__, "detail": str(exc)[:200]}))
    sys.exit(1)

tenant_tables = [str(row[0]) for row in tenant_rows]
rls_violations = [str(row[0]) for row in tenant_rows if not (row[1] and row[2])]
uuid_violations = [str(row[0]) for row in tenant_rows if str(row[3]) != "uuid"]

report = {
    "server_version": server_version,
    "expected_server_version": expected,
    "database": database,
    "pgvector_version": None if vector is None else str(vector[0]),
    "schema_version": schema_version,
    "tenant_tables": tenant_tables,
    "tenant_tables_missing_forced_rls": rls_violations,
    "tenant_tables_with_non_uuid_tenant_id": uuid_violations,
}

if expected and not server_version.startswith(expected):
    report.update(status="failed", code="DEPENDENCY_UNAVAILABLE",
                  message="running PostgreSQL version does not match the pinned production version")
    print(json.dumps(report))
    sys.exit(1)
if vector is None:
    report.update(status="failed", code="DEPENDENCY_UNAVAILABLE",
                  message="pgvector extension is not installed in this database")
    print(json.dumps(report))
    sys.exit(1)
if schema_version is None:
    report.update(status="failed", code="DEPENDENCY_UNAVAILABLE",
                  message="_schema_version is empty after migration")
    print(json.dumps(report))
    sys.exit(1)
if rls_violations:
    report.update(status="failed", code="DEPENDENCY_UNAVAILABLE",
                  message="tenant tables exist without RLS enabled and forced")
    print(json.dumps(report))
    sys.exit(1)
if uuid_violations:
    report.update(status="failed", code="DEPENDENCY_UNAVAILABLE",
                  message="tenant tables use a non-UUID tenant_id")
    print(json.dumps(report))
    sys.exit(1)

report.update(status="passed")
print(json.dumps(report))
PY
) || { printf '{"status":"failed","service":"migrate","code":"MIGRATION_FAILED","message":"post-migration verification failed","details":%s}\n' \
    "$(printf '%s' "$verification" | tr -d '\r\n')" >&2; exit 1; }

printf '{"status":"passed","service":"migrate","target":"%s:%s/%s","verification":%s}\n' \
    "$(json_escape "$POSTGRES_HOST")" "$(json_escape "$POSTGRES_PORT")" \
    "$(json_escape "$POSTGRES_DB")" "$(printf '%s' "$verification" | tr -d '\r\n')"

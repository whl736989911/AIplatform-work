#!/bin/sh
# =============================================================================
# First-admin bootstrap (optional, profile `bootstrap`, run once).
#
#   docker compose -f deploy/compose.production.yml --env-file deploy/.env \
#     --profile bootstrap run --rm bootstrap
#
# Creates the initial administrator only when the deployment has no users at
# all. The password is read from a mounted secret file and handed to the
# application's own offline user-creation path (which enforces the password
# policy); it never appears in argv, environment, logs, or this script's output.
#
# Idempotent: an existing deployment is reported as skipped, never modified.
# =============================================================================
set -eu

fail() {
    printf '{"status":"failed","service":"bootstrap","code":"%s","message":"%s","details":{%s}}\n' \
        "$1" "$2" "$3" >&2
    exit 1
}

username="${OCTOP_ADMIN_USERNAME:-admin}"
display_name="${OCTOP_ADMIN_DISPLAY_NAME:-}"
password_file="${OCTOP_ADMIN_PASSWORD_FILE:-/run/secrets/octop_admin_password}"
pg_password_file="${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}"

[ -f "$pg_password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "PostgreSQL password secret file is missing" "\"path\":\"${pg_password_file}\""
[ -f "$password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "initial administrator password secret file is missing" "\"path\":\"${password_file}\""
[ -s "$password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "initial administrator password secret file is empty" "\"path\":\"${password_file}\""

postgres_password=$(tr -d '\r\n' <"$pg_password_file")
[ -n "$postgres_password" ] || fail DEPENDENCY_UNAVAILABLE \
    "PostgreSQL password secret file is empty" "\"path\":\"${pg_password_file}\""

OCTOP_DATABASE_DRIVER=postgresql
OCTOP_DATABASE_URL=$(OCTOP_DSN_USER="${POSTGRES_USER:?POSTGRES_USER is required}" \
    OCTOP_DSN_PASSWORD="$postgres_password" \
    OCTOP_DSN_HOST="${POSTGRES_HOST:?POSTGRES_HOST is required}" \
    OCTOP_DSN_PORT="${POSTGRES_PORT:-5432}" \
    OCTOP_DSN_DATABASE="${POSTGRES_DB:?POSTGRES_DB is required}" \
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

OCTOP_BOOTSTRAP_USERNAME="$username" \
    OCTOP_BOOTSTRAP_DISPLAY_NAME="$display_name" \
    OCTOP_BOOTSTRAP_PASSWORD_FILE="$password_file" \
    python3 - <<'PY'
import json
import os
import pathlib
import sys

from octop.cli.support.offline_ops import admin_overview_offline, create_user_offline
from octop.infra.errors import OctopError

username = os.environ["OCTOP_BOOTSTRAP_USERNAME"]
display_name = os.environ.get("OCTOP_BOOTSTRAP_DISPLAY_NAME") or None
password = pathlib.Path(os.environ["OCTOP_BOOTSTRAP_PASSWORD_FILE"]).read_text(encoding="utf-8").strip()
if not password:
    print(json.dumps({"status": "failed", "code": "DEPENDENCY_UNAVAILABLE",
                      "message": "administrator password secret is empty"}))
    sys.exit(1)

overview = admin_overview_offline()
existing = overview["users"]
if existing:
    print(json.dumps({
        "status": "skipped",
        "service": "bootstrap",
        "reason": "deployment already has users",
        "users": [user["username"] for user in existing],
    }))
    sys.exit(0)

try:
    create_user_offline(
        username=username,
        password=password,
        role="admin",
        display_name=display_name,
        email=None,
    )
except OctopError as exc:
    print(json.dumps({"status": "failed", "code": exc.code, "message": exc.message}))
    sys.exit(1)

print(json.dumps({
    "status": "passed",
    "service": "bootstrap",
    "username": username,
    "role": "admin",
    "note": "change the password from the dashboard after first login",
}))
PY

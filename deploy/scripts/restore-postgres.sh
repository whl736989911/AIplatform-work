#!/usr/bin/env bash
# =============================================================================
# PostgreSQL restore (host side, explicit, fail closed).
#
#   deploy/scripts/restore-postgres.sh <archive.dump> --scratch
#   deploy/scripts/restore-postgres.sh <archive.dump> --target <database> [--yes]
#
# Guard rails, in order:
#   * OCTOP_ALLOW_RESTORE must be "yes" (default is "no" in the sample env);
#   * the target database must be named explicitly — there is no default;
#   * restoring over the live OCTOP database additionally requires
#     --allow-live-overwrite AND --yes, and terminates live sessions first;
#   * the archive sha256 is checked against its .json sidecar when present;
#   * pg_restore runs with --clean --if-exists on an existing target, so a
#     failure leaves a visible error instead of a half-applied silent merge.
#
# Everything runs inside the postgres container: the host needs no client tools.
# =============================================================================
set -euo pipefail

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=./lib.sh
. "${SELF_DIR}/lib.sh"

load_env_file

ARCHIVE=""
TARGET=""
SCRATCH=0
ASSUME_YES=0
ALLOW_LIVE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --scratch)
            SCRATCH=1
            shift
            ;;
        --target)
            TARGET="${2:-}"
            shift 2
            ;;
        --yes)
            ASSUME_YES=1
            shift
            ;;
        --allow-live-overwrite)
            ALLOW_LIVE=1
            shift
            ;;
        -*) fail DEPLOYMENT_POLICY "unknown option" "option=$1" ;;
        *)
            if [ -n "$ARCHIVE" ]; then
                fail DEPLOYMENT_POLICY "only one archive may be restored per run" "archive=$1"
            fi
            ARCHIVE="$1"
            shift
            ;;
    esac
done

[ "${OCTOP_ALLOW_RESTORE:-no}" = "yes" ] || fail DEPLOYMENT_POLICY \
    "restore is disabled; set OCTOP_ALLOW_RESTORE=yes in the environment file to enable it" \
    "OCTOP_ALLOW_RESTORE=${OCTOP_ALLOW_RESTORE:-no}"

[ -n "$ARCHIVE" ] || fail DEPLOYMENT_POLICY "an archive path is required"
[ -f "$ARCHIVE" ] || fail DEPLOYMENT_POLICY "archive not found" "archive=$ARCHIVE"
[ -s "$ARCHIVE" ] || fail DEPLOYMENT_POLICY "archive is empty" "archive=$ARCHIVE"

if [ "$SCRATCH" -eq 1 ]; then
    TARGET="${OCTOP_RESTORE_SCRATCH_DB:-octop_restore_check}"
    ALLOW_LIVE=0
fi
[ -n "$TARGET" ] || fail DEPLOYMENT_POLICY \
    "a target database is required (--target <database> or --scratch)"

if [ "$TARGET" = "$POSTGRES_DB" ]; then
    [ "$ALLOW_LIVE" -eq 1 ] || fail DEPLOYMENT_POLICY \
        "refusing to overwrite the live database without --allow-live-overwrite" \
        "target=$TARGET"
    [ "$ASSUME_YES" -eq 1 ] || fail DEPLOYMENT_POLICY \
        "overwriting the live database additionally requires --yes" "target=$TARGET"
fi

case "$TARGET" in
    *[!A-Za-z0-9_]*) fail DEPLOYMENT_POLICY "target database name contains unsupported characters" "target=$TARGET" ;;
esac

if ! service_running postgres; then
    fail DEPENDENCY_UNAVAILABLE "postgres service is not running" "service=postgres"
fi

# --- integrity -----------------------------------------------------------------
expected_sha=""
if [ -f "${ARCHIVE}.json" ]; then
    expected_sha=$(sed -n 's/.*"sha256":"\([0-9a-f]\{64\}\)".*/\1/p' "${ARCHIVE}.json" | head -n 1)
fi
if [ -n "$expected_sha" ]; then
    observed_sha=$(sha256sum "$ARCHIVE" | cut -d' ' -f1)
    if [ "$observed_sha" != "$expected_sha" ]; then
        fail DEPENDENCY_UNAVAILABLE "archive checksum does not match its sidecar" \
            "archive=$(basename "$ARCHIVE")"
    fi
else
    log "no sha256 sidecar found for $(basename "$ARCHIVE"); integrity is not independently verified"
fi

# --- preflight: the archive must parse before anything is dropped --------------
# The listing is captured once and matched with a shell pattern: piping into
# `grep -q` would let grep exit early, SIGPIPE the producer, and -- under
# `pipefail` -- report a readable archive as broken.
archive_listing=$(compose exec -T postgres pg_restore --list <"$ARCHIVE" 2>"${ARCHIVE}.list.err") || {
    _detail="$(tr -d '\r\n' <"${ARCHIVE}.list.err" | tail -c 400)"
    rm -f "${ARCHIVE}.list.err"
    fail DEPENDENCY_UNAVAILABLE "archive is not a readable pg_dump custom archive" \
        "detail=${_detail}"
}
rm -f "${ARCHIVE}.list.err"
case "$archive_listing" in
    *"Archive created at"*) ;;
    *)
        fail DEPENDENCY_UNAVAILABLE "archive is not a readable pg_dump custom archive" \
            "archive=$(basename "$ARCHIVE")"
        ;;
esac

log "restoring $(basename "$ARCHIVE") into database ${TARGET}"

if [ "$TARGET" = "$POSTGRES_DB" ] && [ "$ALLOW_LIVE" -eq 1 ]; then
    compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${TARGET}' AND pid <> pg_backend_pid();" \
        >/dev/null
fi

compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
    "DROP DATABASE IF EXISTS ${TARGET};" >/dev/null
compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
    "CREATE DATABASE ${TARGET};" >/dev/null

if ! compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$TARGET" \
    --no-owner --no-privileges --exit-on-error <"$ARCHIVE"; then
    fail DEPENDENCY_UNAVAILABLE "pg_restore failed; the target database is incomplete" "target=$TARGET"
fi

verification=$(compose exec -T postgres psql -U "$POSTGRES_USER" -d "$TARGET" -tAc \
    "SELECT (SELECT version FROM _schema_version),
            (SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')),
            (SELECT count(*) FROM pg_extension WHERE extname = 'vector');" 2>/dev/null \
    | tr -d '\r' || true)
normalized=$(printf '%s' "$verification" | tr -d '[:space:]')
IFS='|' read -r restored_schema_version restored_tables restored_vector <<EOF
$normalized
EOF

case "${restored_schema_version:-}" in
    '' | *[!0-9]*) fail DEPENDENCY_UNAVAILABLE \
        "restored database has no readable _schema_version" "target=$TARGET" ;;
esac
if [ "${restored_vector:-0}" -lt 1 ]; then
    fail DEPENDENCY_UNAVAILABLE "restored database is missing the pgvector extension" "target=$TARGET"
fi

printf '{"status":"passed","service":"restore","archive":"%s","target":"%s","scratch":%s,"schema_version":%s,"tables":%s,"pgvector":true,"integrity_verified":%s,"sha256":"%s"}\n' \
    "$(json_escape "$(basename "$ARCHIVE")")" "$(json_escape "$TARGET")" \
    "$([ "$SCRATCH" -eq 1 ] && printf true || printf false)" \
    "$restored_schema_version" "${restored_tables:-0}" \
    "$([ -n "$expected_sha" ] && printf true || printf false)" "$expected_sha"

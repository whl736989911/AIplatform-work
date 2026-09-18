#!/usr/bin/env bash
# =============================================================================
# PostgreSQL backup (host side, explicit and fail closed).
#
#   deploy/scripts/backup-postgres.sh [--label <label>] [--no-prune]
#
#   dump      pg_dump --format=custom streamed out of the postgres container
#   verify    pg_restore --list must parse the archive and show real objects
#   evidence  <archive>.json sidecar: sha256, size, schema_version, server_version
#   retention archives older than OCTOP_BACKUP_RETENTION_DAYS are pruned, but
#             never below OCTOP_BACKUP_RETENTION_MIN_COUNT newest archives
#   offsite   optional OCTOP_BACKUP_S3_URL upload, verified with a HEAD request
#
# Any failing step aborts the run, removes the partial archive, and exits
# non-zero with a machine-readable error: a half-written backup is never left
# where a restore could mistake it for a good one.
# =============================================================================
set -euo pipefail

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=./lib.sh
. "${SELF_DIR}/lib.sh"

load_env_file

BACKUP_DIR="${OCTOP_BACKUP_DIR:-${DEPLOY_DIR}/backups}"
RETENTION_DAYS="${OCTOP_BACKUP_RETENTION_DAYS:-14}"
MIN_COUNT="${OCTOP_BACKUP_RETENTION_MIN_COUNT:-7}"
S3_URL="${OCTOP_BACKUP_S3_URL:-}"
LABEL=""
PRUNE=1

while [ $# -gt 0 ]; do
    case "$1" in
        --label)
            LABEL="${2:-}"
            shift 2
            ;;
        --no-prune)
            PRUNE=0
            shift
            ;;
        *)
            fail DEPLOYMENT_POLICY "unknown argument" "argument=$1"
            ;;
    esac
done

case "$RETENTION_DAYS" in
    '' | *[!0-9]*) fail DEPLOYMENT_POLICY "OCTOP_BACKUP_RETENTION_DAYS must be an integer" "value=$RETENTION_DAYS" ;;
esac
case "$MIN_COUNT" in
    '' | *[!0-9]*) fail DEPLOYMENT_POLICY "OCTOP_BACKUP_RETENTION_MIN_COUNT must be an integer" "value=$MIN_COUNT" ;;
esac

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
suffix=""
[ -n "$LABEL" ] && suffix="-${LABEL}"
archive="${BACKUP_DIR}/${POSTGRES_DB}-${timestamp}${suffix}.dump"
partial="${archive}.partial"

if ! service_running postgres; then
    fail DEPENDENCY_UNAVAILABLE "postgres service is not running; start the stack before backing up" \
        "service=postgres"
fi

log "dumping ${POSTGRES_DB} from container service postgres"
if ! compose exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --format=custom --no-owner --no-privileges --compress=6 >"$partial" 2>"${partial}.err"; then
    _detail="$(tr -d '\r\n' <"${partial}.err" | tail -c 400)"
    rm -f "$partial" "${partial}.err"
    fail DEPENDENCY_UNAVAILABLE "pg_dump failed" "detail=${_detail}"
fi
rm -f "${partial}.err"

if [ ! -s "$partial" ]; then
    rm -f "$partial"
    fail DEPENDENCY_UNAVAILABLE "pg_dump produced an empty archive" "target=${POSTGRES_HOST:-postgres}"
fi

# --- verify the archive parses and contains real objects ---------------------
table_entries=$(compose exec -T postgres pg_restore --list <"$partial" 2>/dev/null \
    | grep -c 'TABLE DATA' || true)
if [ "${table_entries:-0}" -lt 1 ]; then
    rm -f "$partial"
    fail DEPENDENCY_UNAVAILABLE "archive verification failed: no TABLE DATA entries found" \
        "archive=$(basename "$partial")"
fi
if ! compose exec -T postgres pg_restore --list <"$partial" 2>/dev/null | grep -q 'Archive created at'; then
    rm -f "$partial"
    fail DEPENDENCY_UNAVAILABLE "archive verification failed: missing archive header" \
        "archive=$(basename "$partial")"
fi

schema_version=$(compose exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc 'SELECT version FROM _schema_version' 2>/dev/null \
    | tr -d '\r\n' || true)
case "$schema_version" in
    '' | *[!0-9]*)
        rm -f "$partial"
        fail DEPENDENCY_UNAVAILABLE "cannot read _schema_version; refusing to record an unverified backup" \
            "observed=${schema_version}"
        ;;
esac

server_version=$(compose exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT current_setting('server_version')" 2>/dev/null \
    | tr -d '\r\n' || true)

mv "$partial" "$archive"
chmod 600 "$archive"
sha256=$(sha256sum "$archive" | cut -d' ' -f1)
size_bytes=$(stat -c '%s' "$archive")

cat >"${archive}.json" <<EOF
{"schema_version":${schema_version},"database":"${POSTGRES_DB}","server_version":"${server_version}","created_at":"${timestamp}","size_bytes":${size_bytes},"sha256":"${sha256}","table_data_entries":${table_entries},"format":"pg_dump-custom"}
EOF
chmod 600 "${archive}.json"

# --- retention ---------------------------------------------------------------
pruned=""
if [ "$PRUNE" -eq 1 ]; then
    mapfile -t archives < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -printf '%T@ %p\n' 2>/dev/null | sort -rn | awk '{print $2}')
    index=0
    for candidate in "${archives[@]:-}"; do
        [ -n "$candidate" ] || continue
        index=$((index + 1))
        if [ "$index" -le "$MIN_COUNT" ]; then
            continue
        fi
        if [ -n "$(find "$candidate" -maxdepth 0 -mtime +"$RETENTION_DAYS" 2>/dev/null)" ]; then
            rm -f "$candidate" "${candidate}.json"
            pruned="${pruned}${pruned:+,}\"$(json_escape "$(basename "$candidate")")\""
        fi
    done
fi

# --- optional offsite copy ---------------------------------------------------
offsite='"configured":false'
if [ -n "$S3_URL" ]; then
    if ! command -v aws >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "OCTOP_BACKUP_S3_URL is set but the aws CLI is not installed" \
            "url=${S3_URL}"
    fi
    if ! aws s3 cp "$archive" "${S3_URL%/}/$(basename "$archive")" --only-show-errors >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "offsite upload failed" "url=${S3_URL}"
    fi
    if ! aws s3 cp "${archive}.json" "${S3_URL%/}/$(basename "${archive}.json")" --only-show-errors >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "offsite sidecar upload failed" "url=${S3_URL}"
    fi
    bucket_and_key="${S3_URL#s3://}"
    bucket="${bucket_and_key%%/*}"
    key_prefix=""
    case "$bucket_and_key" in
        */*) key_prefix="${bucket_and_key#*/}/" ;;
    esac
    if ! aws s3api head-object \
        --bucket "$bucket" \
        --key "${key_prefix}$(basename "$archive")" >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "offsite copy could not be verified with a HEAD request" "url=${S3_URL}"
    fi
    offsite="\"configured\":true,\"verified\":true"
fi

printf '{"status":"passed","service":"backup","archive":"%s","sha256":"%s","size_bytes":%s,"schema_version":%s,"server_version":"%s","table_data_entries":%s,"pruned":[%s],"offsite":{%s},"retention":{"days":%s,"min_count":%s}}\n' \
    "$(json_escape "$(basename "$archive")")" "$sha256" "$size_bytes" "$schema_version" \
    "$(json_escape "$server_version")" "$table_entries" "$pruned" "$offsite" \
    "$RETENTION_DAYS" "$MIN_COUNT"

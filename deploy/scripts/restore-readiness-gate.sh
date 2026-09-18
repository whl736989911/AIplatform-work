#!/usr/bin/env bash
# =============================================================================
# Restore-readiness gate (host side).
#
#   deploy/scripts/restore-readiness-gate.sh [--archive <path>]
#
# Answers one question: if this deployment died right now, could it be brought
# back? It passes only when every one of these is true:
#
#   backup_freshness   newest archive is younger than OCTOP_BACKUP_MAX_AGE_HOURS
#   backup_retention   no archive older than OCTOP_BACKUP_RETENTION_DAYS survives
#   restore_drill      the newest archive restores into a scratch database with a
#                      readable _schema_version, real tables, and pgvector, and
#                      the scratch database is dropped again
#   schema_currency    the archive's schema_version equals the live database's
#   dependency_gate    `octop workbuddy dependencies` reports no failure, every
#                      required dependency passed, and any blocked dependency is
#                      explicitly allow-listed (Vault/object storage/BGE gates)
#   app_health         the running app answers /api/health
#   deployment_policy  verify-deployment-policy.sh passes
#
# Anything that cannot be evaluated is a failure. The gate never restores into
# the live database and never modifies deployment state.
# =============================================================================
set -euo pipefail

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=./lib.sh
. "${SELF_DIR}/lib.sh"

load_env_file

BACKUP_DIR="${OCTOP_BACKUP_DIR:-${DEPLOY_DIR}/backups}"
MAX_AGE_HOURS="${OCTOP_BACKUP_MAX_AGE_HOURS:-26}"
RETENTION_DAYS="${OCTOP_BACKUP_RETENTION_DAYS:-14}"
REQUIRED_DEPENDENCIES="${OCTOP_REQUIRED_DEPENDENCIES:-postgresql,redis}"
ALLOWED_BLOCKED="${OCTOP_ALLOWED_BLOCKED:-object_storage,vault,bge_m3}"
SCRATCH_DB="${OCTOP_RESTORE_SCRATCH_DB:-octop_restore_check}"
ARCHIVE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --archive)
            ARCHIVE="${2:-}"
            shift 2
            ;;
        *) fail DEPLOYMENT_POLICY "unknown argument" "argument=$1" ;;
    esac
done

CHECKS=""
add_check() {
    _name="$1"
    _status="$2"
    _detail="$3"
    if [ -n "$CHECKS" ]; then
        CHECKS="${CHECKS},"
    fi
    CHECKS="${CHECKS}{\"name\":\"$(json_escape "$_name")\",\"status\":\"${_status}\",\"detail\":\"$(json_escape "$_detail")\"}"
}

verdict="passed"

# --- newest archive ----------------------------------------------------------
newest=""
newest_mtime=0
if [ -d "$BACKUP_DIR" ]; then
    while IFS= read -r candidate; do
        [ -n "$candidate" ] || continue
        mtime=$(stat -c '%Y' "$candidate" 2>/dev/null || printf '0')
        if [ "$mtime" -gt "$newest_mtime" ]; then
            newest="$candidate"
            newest_mtime="$mtime"
        fi
    done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' 2>/dev/null)
fi
if [ -n "$ARCHIVE" ]; then
    [ -f "$ARCHIVE" ] || fail DEPLOYMENT_POLICY "requested archive not found" "archive=$ARCHIVE"
    newest="$ARCHIVE"
    newest_mtime=$(stat -c '%Y' "$ARCHIVE")
fi

if [ -z "$newest" ]; then
    add_check backup_freshness failed "no backup archive found in ${BACKUP_DIR}"
    verdict="failed"
else
    age_hours=$(( ( $(date -u '+%s') - newest_mtime ) / 3600 ))
    if [ "$age_hours" -le "$MAX_AGE_HOURS" ]; then
        add_check backup_freshness passed "newest archive $(basename "$newest") is ${age_hours}h old (limit ${MAX_AGE_HOURS}h)"
    else
        add_check backup_freshness failed "newest archive $(basename "$newest") is ${age_hours}h old (limit ${MAX_AGE_HOURS}h)"
        verdict="failed"
    fi
fi

# --- retention ----------------------------------------------------------------
stale=""
if [ -d "$BACKUP_DIR" ]; then
    stale=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name '*.dump' -mtime +"$RETENTION_DAYS" -printf '%f ' 2>/dev/null || true)
fi
if [ -z "$stale" ]; then
    add_check backup_retention passed "no archive older than ${RETENTION_DAYS} days remains; pruning is enforced"
else
    add_check backup_retention failed "archives older than ${RETENTION_DAYS} days were not pruned: ${stale}"
    verdict="failed"
fi

# --- restore drill into a scratch database ------------------------------------
if [ -n "$newest" ]; then
    if OCTOP_ALLOW_RESTORE=yes "$SELF_DIR/restore-postgres.sh" "$newest" --scratch >/tmp/restore-drill.json 2>/tmp/restore-drill.err; then
        drill_schema=$(sed -n 's/.*"schema_version":\([0-9]*\).*/\1/p' /tmp/restore-drill.json | head -n 1)
        add_check restore_drill passed "restored $(basename "$newest") into ${SCRATCH_DB} and verified schema/pgvector"
    else
        detail=$(tr -d '\r\n' < /tmp/restore-drill.err | tail -c 300)
        add_check restore_drill failed "restore drill failed: ${detail}"
        verdict="failed"
        drill_schema=""
    fi
    compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 \
        -c "DROP DATABASE IF EXISTS ${SCRATCH_DB};" >/dev/null 2>&1 || true

    live_schema=$(compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
        'SELECT version FROM _schema_version' 2>/dev/null | tr -d '\r\n' || true)
    if [ -n "${drill_schema:-}" ] && [ "$drill_schema" = "$live_schema" ]; then
        add_check schema_currency passed "archive and live database both report schema version ${live_schema}"
    else
        add_check schema_currency failed \
            "archive schema_version=${drill_schema:-<unknown>} differs from live schema_version=${live_schema:-<unknown>}: take a fresh backup after migrating"
        verdict="failed"
    fi
else
    add_check restore_drill failed "no archive available to restore"
    add_check schema_currency failed "no archive available to compare against"
    verdict="failed"
fi

# --- dependency probe ---------------------------------------------------------
if service_running app; then
    if compose exec -T app env \
        OCTOP_REQUIRED_DEPENDENCIES="$REQUIRED_DEPENDENCIES" \
        OCTOP_ALLOWED_BLOCKED="$ALLOWED_BLOCKED" \
        python3 - <<'PY' >/tmp/dependency-gate.json 2>/tmp/dependency-gate.err
import json
import os
import sys

from octop.infra.workbuddy import probe_dependencies

required = {name for name in os.environ["OCTOP_REQUIRED_DEPENDENCIES"].split(",") if name}
allowed_blocked = {name for name in os.environ["OCTOP_ALLOWED_BLOCKED"].split(",") if name}

result = probe_dependencies()
problems = []
for check in result["checks"]:
    name, status = check["name"], check["status"]
    if status == "failed":
        problems.append(f"{name}: failed ({check['detail']})")
        continue
    if status == "blocked" and name not in allowed_blocked:
        problems.append(f"{name}: blocked but not allow-listed ({check['detail']})")
        continue
    if name in required and status != "passed":
        problems.append(f"{name}: required dependency is {status} ({check['detail']})")

print(json.dumps({
    "status": "failed" if problems else "passed",
    "checks": [{"name": check["name"], "status": check["status"]} for check in result["checks"]],
    "problems": problems,
}))
sys.exit(1 if problems else 0)
PY
    then
        add_check dependency_gate passed \
            "required (${REQUIRED_DEPENDENCIES}) passed; blocked entries limited to (${ALLOWED_BLOCKED})"
    else
        detail=$(tr -d '\r\n' < /tmp/dependency-gate.err | tail -c 300)
        problems=$(tr -d '\r\n' < /tmp/dependency-gate.json | tail -c 300)
        add_check dependency_gate failed "${detail}${problems}"
        verdict="failed"
    fi
else
    add_check dependency_gate failed "app service is not running; dependencies cannot be probed"
    verdict="failed"
fi

# --- app health ---------------------------------------------------------------
if service_running app; then
    if compose exec -T app curl -fsS http://127.0.0.1:8088/api/health >/dev/null 2>&1; then
        add_check app_health passed "app answers /api/health"
    else
        add_check app_health failed "app does not answer /api/health"
        verdict="failed"
    fi
else
    add_check app_health failed "app service is not running"
    verdict="failed"
fi

# --- deployment policy --------------------------------------------------------
if "$SELF_DIR/verify-deployment-policy.sh" >/tmp/policy-gate.json 2>/tmp/policy-gate.err; then
    add_check deployment_policy passed "rendered stack satisfies the deployment policy gate"
else
    detail=$(tr -d '\r\n' < /tmp/policy-gate.err | tail -c 300)
    add_check deployment_policy failed "policy gate failed: ${detail}"
    verdict="failed"
fi

printf '{"status":"%s","service":"restore-readiness-gate","archive":"%s","max_age_hours":%s,"retention_days":%s,"checks":[%s]}\n' \
    "$verdict" "$(json_escape "$(basename "${newest:-none}")")" "$MAX_AGE_HOURS" "$RETENTION_DAYS" "$CHECKS"

[ "$verdict" = "passed" ]

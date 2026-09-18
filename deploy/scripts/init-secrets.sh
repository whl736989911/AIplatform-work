#!/usr/bin/env bash
# =============================================================================
# Provision the deployment's secret files (host side, run once).
#
#   deploy/scripts/init-secrets.sh [--force]
#
# Creates deploy/secrets/postgres_password and deploy/secrets/redis_password
# with 32 bytes of CSPRNG entropy (base64, secret-file safe) and mode 0400, plus
# empty placeholders for the OPTIONAL credentials. Empty optional files are the
# explicit "not configured" state: compose mounts them, and the entrypoints fail
# closed when a feature is enabled while its file is empty.
#
# Existing files are never overwritten without --force.
# =============================================================================
set -euo pipefail

DEPLOY_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SECRETS_DIR="${OCTOP_SECRETS_DIR:-${DEPLOY_DIR}/secrets}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

rand_secret() {
    # 32 bytes CSPRNG → base64; only characters accepted by redis validation.
    head -c 32 /dev/urandom | base64 | tr -d '\r\n='
}

write_secret() {
    _name="$1"
    _value="$2"
    _path="${SECRETS_DIR}/${_name}"
    if [ -e "$_path" ] && [ "$FORCE" -ne 1 ]; then
        printf '[octop-deploy] keep existing %s\n' "$_name" >&2
        return 0
    fi
    umask 077
    printf '%s' "$_value" >"$_path"
    chmod 400 "$_path"
    printf '[octop-deploy] wrote %s (mode 0400, %s bytes)\n' "$_name" "${#_value}" >&2
}

place_optional() {
    _name="$1"
    _path="${SECRETS_DIR}/${_name}"
    if [ -e "$_path" ]; then
        printf '[octop-deploy] keep existing %s\n' "$_name" >&2
        return 0
    fi
    umask 077
    : >"$_path"
    chmod 400 "$_path"
    printf '[octop-deploy] created empty placeholder %s (set it to enable the feature)\n' "$_name" >&2
}

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

write_secret postgres_password "$(rand_secret)"
write_secret redis_password "$(rand_secret)"
write_secret octop_admin_password "$(rand_secret)"

place_optional aws_access_key_id
place_optional aws_secret_access_key
place_optional aws_session_token
place_optional vault_role_id
place_optional vault_secret_id

printf '{"status":"passed","service":"init-secrets","directory":"%s","required":["postgres_password","redis_password","octop_admin_password"],"optional":["aws_access_key_id","aws_secret_access_key","aws_session_token","vault_role_id","vault_secret_id"],"note":"rotate postgres_password/redis_password only via a full redeploy; empty optional files keep their feature disabled"}\n' \
    "$SECRETS_DIR"

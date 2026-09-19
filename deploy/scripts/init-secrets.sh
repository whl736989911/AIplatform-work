#!/usr/bin/env bash
# =============================================================================
# Provision the deployment's secret files (host side, run once).
#
#   deploy/scripts/init-secrets.sh [--force]
#
# Creates deploy/secrets/postgres_password and deploy/secrets/redis_password
# with 32 bytes of CSPRNG entropy (base64, secret-file safe) and mode 0444, plus
# empty placeholders for the OPTIONAL credentials. Empty optional files are the
# explicit "not configured" state: compose mounts them, and the entrypoints fail
# closed when a feature is enabled while its file is empty.
#
# Existing files are never overwritten without --force.
# =============================================================================
set -euo pipefail

DEPLOY_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
# `OCTOP_SECRETS_DIR` may be relative (`./secrets`, the shipped default): it must
# mean the same directory compose resolves it against — the deployment directory —
# whatever the caller's working directory is.
secrets_dir="${OCTOP_SECRETS_DIR:-secrets}"
case "${secrets_dir#./}" in
    /*) SECRETS_DIR="${secrets_dir#./}" ;;
    *) SECRETS_DIR="${DEPLOY_DIR}/${secrets_dir#./}" ;;
esac
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
    if [ -z "$_value" ]; then
        printf '{"status":"failed","code":"DEPENDENCY_UNAVAILABLE","message":"secret generation produced no value","details":{"name":"%s"}}\n' "$_name" >&2
        exit 1
    fi
    umask 077
    printf '%s' "$_value" >"$_path"
    chmod 444 "$_path"
    printf '[octop-deploy] wrote %s (mode 0444, %s bytes)\n' "$_name" "${#_value}" >&2
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
    chmod 444 "$_path"
    printf '[octop-deploy] created empty placeholder %s (set it to enable the feature)\n' "$_name" >&2
}

mkdir -p "$SECRETS_DIR"
# 0711: the container uids must be able to traverse the directory to read their
# secret file, while the directory stays unlistable for everyone else.  The files
# themselves are 0444 because compose bind-mounts them with their host owner and
# mode intact, and the services run as 999 / 1000 / 10001 — a 0400 root-owned file
# is "permission denied" for every one of them (verified on ext4), which stops the
# whole stack from starting.
chmod 711 "$SECRETS_DIR"

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

#!/bin/sh
# =============================================================================
# One-shot volume ownership repair for the Octop production stack.
#
# Runs once as root with a read-only root filesystem and only CHOWN /
# DAC_OVERRIDE / FOWNER, then exits. Every long-running service starts as a
# non-root uid, so its named volume must already be owned by that uid: this is
# the only place in the stack that holds any elevated capability.
#
# Idempotent and cheap: only the volume root inode is inspected, and a chown is
# issued only on a fresh (or mis-owned) volume.
# =============================================================================
set -eu

json_pair() {
    printf '"%s":{"path":"%s","uid_gid":"%s","action":"%s"}' "$1" "$2" "$3" "$4"
}

OUT=""
CHANGED=0
ADD='
'

add_result() {
    if [ -n "$OUT" ]; then
        OUT="${OUT},"
    fi
    OUT="${OUT}$(json_pair "$1" "$2" "$3" "$4")"
}

handle() {
    _name="$1"
    _uid_gid="$2"
    _path="$3"

    if [ ! -d "$_path" ]; then
        mkdir -p "$_path" || {
            printf '{"status":"failed","code":"DEPENDENCY_UNAVAILABLE","message":"cannot create volume mount point","details":{"path":"%s"}}\n' "$_path" >&2
            exit 1
        }
    fi

    _uid=${_uid_gid%%:*}
    _gid=${_uid_gid##*:}
    _current=$(stat -c '%u:%g' "$_path")
    if [ "$_current" = "$_uid_gid" ]; then
        add_result "$_name" "$_path" "$_uid_gid" "unchanged"
        return 0
    fi
    if ! chown "$_uid_gid" "$_path" 2>/dev/null; then
        printf '{"status":"failed","code":"DEPENDENCY_UNAVAILABLE","message":"cannot chown volume root","details":{"path":"%s","observed":"%s","wanted":"%s"}}\n' \
            "$_path" "$_current" "$_uid_gid" >&2
        exit 1
    fi
    add_result "$_name" "$_path" "$_uid_gid" "chowned"
    CHANGED=$((CHANGED + 1))
}

handle octop_data "/volumes/octop" "${OCTOP_UID:-10001}:${OCTOP_GID:-10001}"
handle postgres_data "/volumes/postgres" "${POSTGRES_UID:-999}:${POSTGRES_GID:-999}"
handle redis_data "/volumes/redis" "${REDIS_UID:-999}:${REDIS_GID:-1000}"
handle vault_data "/volumes/vault" "${VAULT_UID:-100}:${VAULT_GID:-1000}"

printf '{"status":"passed","service":"volume-init","changed":%s,"volumes":{%s}}\n' \
    "$CHANGED" "$OUT"

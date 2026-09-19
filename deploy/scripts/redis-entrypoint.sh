#!/bin/sh
# =============================================================================
# Redis entrypoint: renders the config into tmpfs with the injected password.
#
# The password is read from a mounted secret file, validated against a strict
# character set (so it cannot break out of the `requirepass` directive it is
# substituted into), substituted into a template rendered into tmpfs, and the
# rendered file is deleted from any persistent location. The secret therefore
# never reaches the image, argv, or the process environment.
# =============================================================================
set -eu

fail() {
    printf '{"status":"failed","service":"redis","code":"%s","message":"%s","details":{%s}}\n' \
        "$1" "$2" "$3" >&2
    exit 1
}

password_file="${REDIS_PASSWORD_FILE:-/run/secrets/redis_password}"
template="${REDIS_CONFIG_TEMPLATE:-/opt/octop-deploy/config/redis.conf.template}"
render_dir="${REDIS_RENDER_DIR:-/run/redis}"
render_path="${render_dir}/redis.conf"

[ -f "$password_file" ] || fail DEPENDENCY_UNAVAILABLE \
    "Redis password secret file is missing" "\"path\":\"${password_file}\""
[ -r "$template" ] || fail DEPENDENCY_UNAVAILABLE \
    "Redis config template is missing" "\"path\":\"${template}\""

password=$(tr -d '\r\n' <"$password_file")
[ -n "$password" ] || fail DEPENDENCY_UNAVAILABLE \
    "Redis password secret file is empty" "\"path\":\"${password_file}\""

# Reject anything that cannot be embedded safely in a quoted Redis directive.
case "$password" in
    *[!A-Za-z0-9._~+=/:-]*)
        fail DEPENDENCY_UNAVAILABLE \
            "Redis password contains characters outside the accepted set" \
            '"accepted":"A-Za-z0-9._~+=/:-"'
        ;;
esac
[ "${#password}" -ge 16 ] || fail DEPENDENCY_UNAVAILABLE \
    "Redis password is shorter than 16 characters" "\"length\":\"${#password}\""

maxmemory="${REDIS_MAXMEMORY:-512mb}"
case "$maxmemory" in
    *[!0-9a-z]* | '') fail DEPENDENCY_UNAVAILABLE \
        "REDIS_MAXMEMORY is not a valid size" "\"value\":\"${maxmemory}\"" ;;
esac
policy="${REDIS_MAXMEMORY_POLICY:-noeviction}"
case "$policy" in
    noeviction | allkeys-lru | allkeys-lfu | volatile-lru | volatile-lfu | allkeys-random | volatile-random | volatile-ttl) ;;
    *) fail DEPENDENCY_UNAVAILABLE \
        "REDIS_MAXMEMORY_POLICY is not a valid policy" "\"value\":\"${policy}\"" ;;
esac

mkdir -p "$render_dir"
umask 077
sed -e "s|__REDIS_PASSWORD__|${password}|g" \
    -e "s|__REDIS_MAXMEMORY__|${maxmemory}|g" \
    -e "s|__REDIS_MAXMEMORY_POLICY__|${policy}|g" \
    "$template" >"$render_path"
chmod 600 "$render_path"
unset password

# Fail fast and loudly on an invalid config rather than looping in a crash.
if ! redis-server --version >/dev/null 2>&1; then
    fail DEPENDENCY_UNAVAILABLE "redis-server is not available in this image"
fi

printf '{"status":"starting","service":"redis","config":"%s","maxmemory":"%s","policy":"%s"}\n' \
    "$render_path" "$maxmemory" "$policy"

exec redis-server "$render_path"

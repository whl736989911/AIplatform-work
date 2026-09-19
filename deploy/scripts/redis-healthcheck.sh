#!/bin/sh
# =============================================================================
# Redis health probe.
#
# PING must succeed WITH authentication, so an instance that lost its credential
# or is refusing commands is reported unhealthy instead of "reachable". The
# password is passed through REDISCLI_AUTH rather than argv.
# =============================================================================
set -eu

password_file="${REDIS_PASSWORD_FILE:-/run/secrets/redis_password}"
[ -r "$password_file" ] || {
    printf '{"status":"failed","service":"redis","code":"DEPENDENCY_UNAVAILABLE","message":"password secret file is not readable"}\n' >&2
    exit 1
}

if ! command -v redis-cli >/dev/null 2>&1; then
    printf '{"status":"failed","service":"redis","code":"DEPENDENCY_UNAVAILABLE","message":"redis-cli is not available in this image"}\n' >&2
    exit 1
fi

response=$(REDISCLI_AUTH="$(tr -d '\r\n' <"$password_file")" \
    redis-cli --no-auth-warning -h 127.0.0.1 -p 6379 ping 2>/dev/null || true)

case "$response" in
    PONG) exit 0 ;;
    *)
        printf '{"status":"failed","service":"redis","code":"DEPENDENCY_UNAVAILABLE","message":"authenticated PING failed"}\n' >&2
        exit 1
        ;;
esac

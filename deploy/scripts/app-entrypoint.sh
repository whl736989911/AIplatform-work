#!/bin/sh
# =============================================================================
# Application entrypoint (runs inside the Octop image, non-root uid).
#
# Why this exists instead of the image ENTRYPOINT: the image entrypoint performs
# single-user SQLite bootstrap (`octop init` writing ~/.octop/credential.txt).
# Production runs PostgreSQL only, with schema applied by the `migrate` job, and
# every credential delivered as a mounted secret file. This script therefore:
#
#   1. builds the control-plane DSN and broker URL from secret FILES, so no
#      password appears in the compose file, the image, argv, or the logs. Both
#      URLs are exported into this process's environment (DATABASE_URL,
#      REDIS_URL) because the dependency probe reads them from there; that means
#      `docker inspect` and this container's own uid can read them, which is the
#      known, accepted exposure of running as this uid;
#   2. refuses to start when a required secret is missing or empty;
#   3. wires the private S3-compatible storage adapter and Vault workload
#      identity placeholders from files, failing closed when the operator
#      declared them required but they are absent;
#   4. execs the server in the foreground as PID 1.
# =============================================================================
set -eu

fail() {
    printf '{"status":"failed","code":"%s","message":"%s","details":{%s}}\n' \
        "$1" "$2" "$3" >&2
    exit 1
}

require_file_secret() {
    _label="$1"
    _path="$2"
    [ -f "$_path" ] || fail DEPENDENCY_UNAVAILABLE "${_label} secret file is missing" "\"path\":\"$_path\""
    [ -r "$_path" ] || fail DEPENDENCY_UNAVAILABLE "${_label} secret file is not readable" "\"path\":\"$_path\""
    _value=$(tr -d '\r\n' <"$_path")
    [ -n "$_value" ] || fail DEPENDENCY_UNAVAILABLE "${_label} secret file is empty" "\"path\":\"$_path\""
    printf '%s' "$_value"
}

dsn() {
    OCTOP_DSN_USER="$POSTGRES_USER" \
        OCTOP_DSN_PASSWORD="$1" \
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
))'
}

# --- control plane: PostgreSQL is mandatory, SQLite is never a fallback ------
# The DSN is built only from PostgreSQL parts; OCTOP_DATABASE_DRIVER is pinned
# here so no inherited value can select the SQLite backend.
postgres_password=$(require_file_secret "PostgreSQL password" "${POSTGRES_PASSWORD_FILE:-/run/secrets/postgres_password}")

OCTOP_DATABASE_DRIVER=postgresql
OCTOP_DATABASE_URL=$(dsn "$postgres_password")
DATABASE_URL="$OCTOP_DATABASE_URL"
export OCTOP_DATABASE_DRIVER OCTOP_DATABASE_URL DATABASE_URL
unset postgres_password

# --- broker / rate limiting ---------------------------------------------------
redis_password=""
if [ -n "${REDIS_PASSWORD_FILE:-}" ] && [ -f "${REDIS_PASSWORD_FILE}" ]; then
    redis_password=$(require_file_secret "Redis password" "$REDIS_PASSWORD_FILE")
fi
if [ -n "$redis_password" ]; then
    REDIS_URL=$(OCTOP_REDIS_PASSWORD="$redis_password" \
        OCTOP_REDIS_HOST="${REDIS_HOST:?REDIS_HOST is required}" \
        OCTOP_REDIS_PORT="${REDIS_PORT:-6379}" \
        OCTOP_REDIS_DB="${REDIS_DB:-0}" \
        python3 -c 'import os, urllib.parse as u
print("redis://:%s@%s:%s/%s" % (
    u.quote(os.environ["OCTOP_REDIS_PASSWORD"], safe=""),
    os.environ["OCTOP_REDIS_HOST"],
    os.environ["OCTOP_REDIS_PORT"],
    os.environ["OCTOP_REDIS_DB"],
))')
    export REDIS_URL
    unset redis_password
elif [ "${REDIS_REQUIRED:-true}" = "true" ]; then
    fail DEPENDENCY_UNAVAILABLE "Redis is required by this deployment but no credential was provided" '"path":"/run/secrets/redis_password"'
fi

# --- private S3-compatible storage: adapter config from env, keys from files --
storage_state='"configured":false'
if [ -n "${OBJECT_STORAGE_ENDPOINT:-}" ] || [ -n "${OBJECT_STORAGE_BUCKET:-}" ]; then
    [ -n "${OBJECT_STORAGE_ENDPOINT:-}" ] || fail DEPENDENCY_UNAVAILABLE \
        "OBJECT_STORAGE_ENDPOINT is empty but object storage was declared" '"key":"OBJECT_STORAGE_ENDPOINT"'
    [ -n "${OBJECT_STORAGE_BUCKET:-}" ] || fail DEPENDENCY_UNAVAILABLE \
        "OBJECT_STORAGE_BUCKET is empty but object storage was declared" '"key":"OBJECT_STORAGE_BUCKET"'
    key_id=$(OCTOP_SECRET_OPTIONAL=0 require_file_secret "object storage access key id" "${AWS_ACCESS_KEY_ID_FILE:-/run/secrets/aws_access_key_id}")
    secret=$(OCTOP_SECRET_OPTIONAL=0 require_file_secret "object storage secret access key" "${AWS_SECRET_ACCESS_KEY_FILE:-/run/secrets/aws_secret_access_key}")
    AWS_ACCESS_KEY_ID="$key_id"
    AWS_SECRET_ACCESS_KEY="$secret"
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    unset key_id secret
    if [ -s "${AWS_SESSION_TOKEN_FILE:-/run/secrets/aws_session_token}" ]; then
        AWS_SESSION_TOKEN=$(tr -d '\r\n' <"${AWS_SESSION_TOKEN_FILE}")
        export AWS_SESSION_TOKEN
    fi
    AWS_DEFAULT_REGION="${OBJECT_STORAGE_REGION:-}"
    AWS_REGION="${OBJECT_STORAGE_REGION:-}"
    export AWS_DEFAULT_REGION AWS_REGION
    storage_state='"configured":true'
fi

# --- Vault workload identity placeholders ------------------------------------
vault_state='"configured":false,"required":false'
if [ "${VAULT_WORKLOAD_IDENTITY_REQUIRED:-false}" = "true" ]; then
    [ -n "${VAULT_ADDR:-}" ] || fail DEPENDENCY_UNAVAILABLE \
        "Vault workload identity is required but VAULT_ADDR is empty" '"key":"VAULT_ADDR"'
    role_id=$(require_file_secret "Vault role id" "${VAULT_ROLE_ID_FILE:-/run/secrets/vault_role_id}")
    secret_id=$(require_file_secret "Vault secret id" "${VAULT_SECRET_ID_FILE:-/run/secrets/vault_secret_id}")
    VAULT_ROLE_ID="$role_id"
    VAULT_SECRET_ID="$secret_id"
    export VAULT_ROLE_ID VAULT_SECRET_ID
    unset role_id secret_id
    vault_state='"configured":true,"required":true'
elif [ -s "${VAULT_ROLE_ID_FILE:-/dev/null}" ] && [ -s "${VAULT_SECRET_ID_FILE:-/dev/null}" ]; then
    VAULT_ROLE_ID=$(tr -d '\r\n' <"$VAULT_ROLE_ID_FILE")
    VAULT_SECRET_ID=$(tr -d '\r\n' <"$VAULT_SECRET_ID_FILE")
    export VAULT_ROLE_ID VAULT_SECRET_ID
    vault_state='"configured":true,"required":false'
fi

BIND_HOST="${OCTOP_BIND_HOST:-0.0.0.0}"
BIND_PORT="${OCTOP_PORT:-8088}"
export OCTOP_BIND_HOST="$BIND_HOST" OCTOP_PORT="$BIND_PORT"

# --- which tier this container is --------------------------------------------
# The published topology separates the API (no in-process execution state) from
# the worker that claims and runs executions. Both tiers build the same DSN from
# the same secret files; only the process they exec differs.
DEPLOY_SERVICE="${OCTOP_DEPLOY_SERVICE:-app}"

printf '{"status":"starting","service":"%s","database":"postgresql","object_storage":{%s},"vault":{%s}}\n' \
    "$DEPLOY_SERVICE" "$storage_state" "$vault_state"

if [ "$DEPLOY_SERVICE" = "worker" ]; then
    exec octop workbuddy-worker
fi

exec octop run --host "$BIND_HOST" --port "$BIND_PORT"

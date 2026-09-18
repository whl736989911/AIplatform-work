# =============================================================================
# Shared helpers for the Octop production deployment scripts.
#
# Sourced, never executed. POSIX sh compatible (container scripts run on
# Debian slim where /bin/sh is dash; host scripts may re-use the same helpers).
#
# Two rules hold everywhere:
#   * fail closed — a missing prerequisite stops the operation with a
#     machine-readable error instead of degrading to a fallback;
#   * never print secret material — only paths, names and digests.
# =============================================================================

# --- identity of this deployment ---------------------------------------------
# DEPLOY_DIR: directory holding compose.production.yml (script's parent/parent).
DEPLOY_DIR="${DEPLOY_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
COMPOSE_FILE="${OCTOP_COMPOSE_FILE:-${DEPLOY_DIR}/compose.production.yml}"
ENV_FILE="${OCTOP_ENV_FILE:-${DEPLOY_DIR}/.env}"
SECRETS_DIR="${OCTOP_SECRETS_DIR:-${DEPLOY_DIR}/secrets}"

# --- logging -----------------------------------------------------------------
log() {
    printf '[octop-deploy] %s\n' "$*" >&2
}

# --- machine-readable failures ----------------------------------------------
# json_escape: minimal, dependency-free JSON string escaping (host scripts have
# no guaranteed python, container scripts must not depend on one either).
json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\t/\\t/g' | tr -d '\r\n'
}

# fail <code> <message> [key=value ...]
# Emits {"status":"failed","code":...,"message":...,"details":{...}} on stderr
# and exits non-zero. Codes follow the WorkBuddy dependency contract:
#   DEPENDENCY_UNAVAILABLE — a required external dependency is unreachable,
#                            unconfigured, or rejected the request;
#   MODEL_NOT_CONFIGURED   — a model/embedding dependency is not configured;
#   *_POLICY               — a deployment guard refused the operation.
fail() {
    _code="$1"
    shift
    _message="$1"
    shift
    _details=""
    for _pair in "$@"; do
        _key=${_pair%%=*}
        _value=${_pair#*=}
        if [ -n "$_details" ]; then
            _details="${_details},"
        fi
        _details="${_details}\"$(json_escape "$_key")\":\"$(json_escape "$_value")\""
    done
    printf '{"status":"failed","code":"%s","message":"%s","details":{%s}}\n' \
        "$(json_escape "$_code")" "$(json_escape "$_message")" "$_details" >&2
    exit "${OCTOP_FAIL_EXIT_CODE:-1}"
}

# --- secret files -------------------------------------------------------------
# read_secret <path> <label>: echoes the trimmed secret, or fails closed.
# Empty (but existing) files are treated as "not configured" by callers that
# pass OCTOP_SECRET_OPTIONAL=1; everything else is a hard failure.
read_secret() {
    _path="$1"
    _label="$2"
    if [ ! -f "$_path" ]; then
        fail DEPENDENCY_UNAVAILABLE "${_label} secret file is missing" "path=${_path}"
    fi
    if [ ! -r "$_path" ]; then
        fail DEPENDENCY_UNAVAILABLE "${_label} secret file is not readable" "path=${_path}"
    fi
    _value=$(tr -d '\r\n' <"$_path")
    if [ -z "$_value" ]; then
        if [ "${OCTOP_SECRET_OPTIONAL:-0}" = "1" ]; then
            return 0
        fi
        fail DEPENDENCY_UNAVAILABLE "${_label} secret file is empty" "path=${_path}"
    fi
    printf '%s' "$_value"
}

# --- postgres DSN -------------------------------------------------------------
# dsn_from_parts <user> <password> <host> <port> <database>
# Percent-encodes user/password/database so credentials containing reserved
# characters cannot corrupt the DSN. Uses the interpreter that ships with the
# Octop image; fails closed when it is unavailable.
dsn_from_parts() {
    _user="$1"
    _password="$2"
    _host="$3"
    _port="$4"
    _database="$5"
    if ! command -v python3 >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "python3 is required to build the PostgreSQL DSN"
    fi
    OCTOP_DSN_USER="$_user" \
        OCTOP_DSN_PASSWORD="$_password" \
        OCTOP_DSN_HOST="$_host" \
        OCTOP_DSN_PORT="$_port" \
        OCTOP_DSN_DATABASE="$_database" \
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

# --- redis URL ----------------------------------------------------------------
# redis_url_from_password <password> <host> <port> <db>
redis_url_from_password() {
    _password="$1"
    _host="$2"
    _port="$3"
    _db="$4"
    if ! command -v python3 >/dev/null 2>&1; then
        fail DEPENDENCY_UNAVAILABLE "python3 is required to build the Redis URL"
    fi
    OCTOP_REDIS_PASSWORD="$_password" \
        OCTOP_REDIS_HOST="$_host" \
        OCTOP_REDIS_PORT="$_port" \
        OCTOP_REDIS_DB="$_db" \
        python3 -c 'import os, urllib.parse as u
print("redis://:%s@%s:%s/%s" % (
    u.quote(os.environ["OCTOP_REDIS_PASSWORD"], safe=""),
    os.environ["OCTOP_REDIS_HOST"],
    os.environ["OCTOP_REDIS_PORT"],
    os.environ["OCTOP_REDIS_DB"],
))'
}

# --- host-side compose wrapper ------------------------------------------------
compose() {
    if [ ! -f "$COMPOSE_FILE" ]; then
        fail DEPLOYMENT_POLICY "compose file not found" "path=${COMPOSE_FILE}"
    fi
    if [ ! -f "$ENV_FILE" ]; then
        fail DEPLOYMENT_POLICY "environment file not found (copy production.env.example)" \
            "path=${ENV_FILE}"
    fi
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# load_env_file: export the deployment settings for host scripts, ignoring
# comments/blank lines. Values are used verbatim (no eval).
load_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        fail DEPLOYMENT_POLICY "environment file not found (copy production.env.example)" \
            "path=${ENV_FILE}"
    fi
    while IFS= read -r _line || [ -n "$_line" ]; do
        case "$_line" in
            '' | '#'*) continue ;;
        esac
        _key=${_line%%=*}
        _value=${_line#*=}
        case "$_key" in
            *[!A-Za-z0-9_]*) continue ;;
        esac
        export "$_key=$_value"
    done <"$ENV_FILE"
}

# service_running <service>: 0 when the container is up.
service_running() {
    _state=$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
        ps --status running --services 2>/dev/null | tr -d '\r')
    case " $(printf '%s' "$_state" | tr '\n' ' ') " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

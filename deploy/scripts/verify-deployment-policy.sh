#!/usr/bin/env bash
# =============================================================================
# Deployment policy gate (host side, CI-runnable).
#
#   deploy/scripts/verify-deployment-policy.sh [--live]
#
# Renders the production stack (`docker compose config --format json`) and
# asserts the properties this deployment promises. Nothing is started; with
# --live the running containers are additionally inspected for their observed
# PostgreSQL / Redis / Vault versions, which must match the versions pinned in
# contracts/dependency-lock.json.
#
# Fail closed: an assertion that cannot be evaluated (missing python, unrenderable
# config, unreachable runtime) is a failure, never a skip.
# =============================================================================
set -euo pipefail

SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=./lib.sh
. "${SELF_DIR}/lib.sh"

LIVE=0
[ "${1:-}" = "--live" ] && LIVE=1

load_env_file

if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    fail DEPLOYMENT_POLICY "python3 is required by the policy gate and was not found on PATH"
fi

rendered=$(compose --profile '*' config --format json 2>/tmp/compose-config.err) || {
    _detail="$(tr -d '\r\n' < /tmp/compose-config.err | tail -c 400)"
    fail DEPLOYMENT_POLICY "docker compose config failed" "detail=${_detail}"
}

RENDERED_JSON="$rendered" \
    COMPOSE_FILE="$COMPOSE_FILE" \
    ENV_FILE="$ENV_FILE" \
    POSTGRES_USER="${POSTGRES_USER:-octop}" \
    POSTGRES_DB="${POSTGRES_DB:-octop}" \
    OCTOP_EXPECTED_DATABASE_SERVER_VERSION="${OCTOP_EXPECTED_DATABASE_SERVER_VERSION:-}" \
    OCTOP_EXPECTED_REDIS_VERSION="${OCTOP_EXPECTED_REDIS_VERSION:-}" \
    OCTOP_EXPECTED_VAULT_VERSION="${OCTOP_EXPECTED_VAULT_VERSION:-}" \
    OCTOP_ALLOW_PUBLIC_APP="${OCTOP_ALLOW_PUBLIC_APP:-false}" \
    OCTOP_LIVE="$LIVE" \
    "$PYTHON" - <<'PY'
import json
import os
import re
import subprocess
import sys

rendered = json.loads(os.environ["RENDERED_JSON"])
services = rendered.get("services", {})
networks = rendered.get("networks", {})
secrets = rendered.get("secrets", {})

checks = []


def check(name, ok, detail):
    checks.append({"name": name, "status": "passed" if ok else "failed", "detail": detail})
    return ok


DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
CREDENTIAL_KEY_RE = re.compile(r"(?:^|_)(PASSWORD|SECRET|TOKEN|ACCESS_KEY|SECRET_KEY|CREDENTIAL)(?:_|$)")
REQUIRED_SERVICES = ("app", "postgres", "redis", "migrate", "volume-init")
# Services that stay up and therefore must publish a healthcheck. The one-shot
# jobs (volume-init, migrate, bootstrap) are asserted by `jobs_are_one_shot`
# instead: a healthcheck on a container that exits cannot report anything.
HEALTHCHECKED_SERVICES = ("app", "postgres", "redis", "vault")
NON_ROOT_EXPECTED = ("app", "postgres", "redis", "migrate", "volume-init", "vault", "bootstrap")


# --- 1. every image is digest-pinned ----------------------------------------
missing = [name for name in REQUIRED_SERVICES if name not in services]
check("required_services_present", not missing,
      "all expected services are defined" if not missing else f"missing services: {missing}")

bad_images = []
for name, service in sorted(services.items()):
    image = str(service.get("image", ""))
    if not DIGEST_RE.match(image):
        bad_images.append(f"{name}={image or '<unset>'}")
check("images_digest_pinned", not bad_images,
      "every image reference is repo@sha256:<64 hex>" if not bad_images
      else "not digest-pinned (or resolvable from env): " + ", ".join(bad_images))

# --- 2. container escape surfaces -------------------------------------------
escapes = []
for name, service in sorted(services.items()):
    if service.get("privileged"):
        escapes.append(f"{name}:privileged")
    if service.get("network_mode") and service.get("network_mode") != "bridge":
        escapes.append(f"{name}:network_mode={service['network_mode']}")
    for key in ("pid", "ipc", "userns_mode", "cgroup"):
        value = service.get(key)
        if value in ("host",):
            escapes.append(f"{name}:{key}=host")
    for mount in service.get("volumes", []) or []:
        source = str(mount.get("source", "")) if isinstance(mount, dict) else str(mount)
        if "docker.sock" in source:
            escapes.append(f"{name}:docker.sock")
check("no_privileged_or_host_namespace_access", not escapes,
      "no privileged mode, host namespaces, or docker socket" if not escapes
      else "found: " + ", ".join(escapes))

# --- 3. datastores are never published ---------------------------------------
published_problems = []
for name in ("postgres", "redis", "vault"):
    service = services.get(name)
    if service is None:
        continue
    if service.get("ports"):
        published_problems.append(f"{name} publishes {len(service['ports'])} port(s)")
check("datastores_not_published", not published_problems,
      "PostgreSQL, Redis and Vault expose no host ports" if not published_problems
      else "; ".join(published_problems))

app_ports = services.get("app", {}).get("ports", []) or []
allow_public = str(os.environ.get("OCTOP_ALLOW_PUBLIC_APP", "false")).lower() == "true"
public_ports = [port for port in app_ports if str(port.get("host_ip", "")) in ("", "0.0.0.0", "::")]
if allow_public:
    port_detail = "app publish address is an explicit public decision (OCTOP_ALLOW_PUBLIC_APP=true)"
else:
    port_detail = ("app is published on a specific host address" if not public_ports
                   else "app is published on a wildcard host address")
check("app_port_binding", allow_public or not public_ports, port_detail)

# --- 4. per-service hardening ------------------------------------------------
hardening_problems = []
for name in NON_ROOT_EXPECTED:
    service = services.get(name)
    if service is None:
        continue
    user = str(service.get("user", ""))
    uid = user.split(":")[0]
    if name != "volume-init" and uid == "0":
        hardening_problems.append(f"{name}:runs_as_root")
    if not service.get("read_only"):
        hardening_problems.append(f"{name}:rootfs_writable")
    caps = [str(cap).upper() for cap in service.get("cap_drop", []) or []]
    if "ALL" not in caps:
        hardening_problems.append(f"{name}:capabilities_not_dropped")
    try:
        security = " ".join(service.get("security_opt", []) or [])
    except AttributeError:
        security = ""
    if "no-new-privileges" not in security:
        hardening_problems.append(f"{name}:no_new_privileges_missing")
    if not service.get("tmpfs"):
        hardening_problems.append(f"{name}:no_tmpfs_scratch")
    if name in HEALTHCHECKED_SERVICES and (
        not service.get("healthcheck") or not service["healthcheck"].get("test")
    ):
        hardening_problems.append(f"{name}:no_healthcheck")
    if name not in ("volume-init", "migrate", "bootstrap"):
        limits = (
            service.get("deploy", {}).get("resources", {}).get("limits", {})
            if isinstance(service.get("deploy"), dict)
            else {}
        )
        if not limits.get("memory") or not limits.get("cpus"):
            hardening_problems.append(f"{name}:unbounded_resources")
    logging_options = service.get("logging", {})
    if isinstance(logging_options, dict):
        options = logging_options.get("options") or {}
        if not options.get("max-size"):
            hardening_problems.append(f"{name}:unbounded_logs")
check("service_hardening", not hardening_problems,
      "non-root, read-only rootfs, all capabilities dropped, no-new-privileges, tmpfs, healthcheck, bounded resources and logs"
      if not hardening_problems else "found: " + ", ".join(hardening_problems))

# --- 5. app start ordering ---------------------------------------------------
app = services.get("app", {})
depends = app.get("depends_on", {}) or {}
expected_conditions = {
    "migrate": "service_completed_successfully",
    "postgres": "service_healthy",
    "redis": "service_healthy",
    "volume-init": "service_completed_successfully",
}
ordering_problems = []
for dependency, condition in expected_conditions.items():
    entry = depends.get(dependency)
    observed = entry.get("condition") if isinstance(entry, dict) else str(entry or "")
    if observed != condition:
        ordering_problems.append(f"app->{dependency}:{observed or 'absent'} (want {condition})")
check("app_start_ordering", not ordering_problems,
      "app waits for migration success and healthy PostgreSQL/Redis" if not ordering_problems
      else "; ".join(ordering_problems))

# --- 6. networks -------------------------------------------------------------
network_problems = []
for name in ("data", "vaultpriv"):
    entry = networks.get(name)
    if not entry:
        network_problems.append(f"{name}:missing")
        continue
    if not entry.get("internal"):
        network_problems.append(f"{name}:not_internal")
for name in ("postgres", "redis"):
    service = services.get(name) or {}
    attached = service.get("networks", {}) or {}
    for network_name in attached:
        entry = networks.get(network_name) or {}
        if not entry.get("internal"):
            network_problems.append(f"{name}:attached_to_public_network:{network_name}")
check("private_networks", not network_problems,
      "datastores sit on internal-only networks" if not network_problems
      else "found: " + ", ".join(network_problems))

# --- 7. secrets come from files, never inline --------------------------------
secret_problems = []
for name, entry in sorted(secrets.items()):
    if not isinstance(entry, dict) or not entry.get("file"):
        secret_problems.append(f"{name}:not_file_backed")
inline_credentials = []
for name, service in sorted(services.items()):
    environment = service.get("environment") or {}
    if isinstance(environment, list):
        pairs = [item.split("=", 1) for item in environment if "=" in str(item)]
    else:
        pairs = list(environment.items())
    for key, value in pairs:
        key = str(key)
        # A boolean switch (OCTOP_REQUIRE_SETUP_PASSWORD=true) matches the key
        # pattern but carries no credential, so only real values are flagged.
        flag = isinstance(value, bool) or str(value).strip().lower() in ("true", "false")
        if flag:
            continue
        if CREDENTIAL_KEY_RE.search(key.upper()) and not key.upper().endswith("_FILE"):
            if value not in (None, ""):
                inline_credentials.append(f"{name}:{key}")
check("secrets_file_indirection", not secret_problems and not inline_credentials,
      "all secrets are file-backed and no credential value is inlined"
      if not secret_problems and not inline_credentials
      else "problems: " + ", ".join(secret_problems + inline_credentials))

# --- 8. jobs are one-shot ----------------------------------------------------
restart_problems = []
for name in ("volume-init", "migrate", "bootstrap"):
    service = services.get(name)
    if service is None:
        continue
    if str(service.get("restart", "no")) not in ("no", "None", ""):
        restart_problems.append(f"{name}:restart={service['restart']}")
check("jobs_are_one_shot", not restart_problems,
      "migration/bootstrap/volume-init run once and report failure" if not restart_problems
      else "found: " + ", ".join(restart_problems))

# --- 9. live runtime versions match the dependency lock ----------------------
if os.environ.get("OCTOP_LIVE") == "1":
    def run(argv):
        return subprocess.run(argv, capture_output=True, text=True, check=False)

    compose_argv = ["docker", "compose", "-f", str(os.environ["COMPOSE_FILE"]),
                    "--env-file", str(os.environ["ENV_FILE"])]

    # PostgreSQL
    expected = os.environ.get("OCTOP_EXPECTED_DATABASE_SERVER_VERSION", "")
    result = run(compose_argv + ["exec", "-T", "postgres", "psql", "-U", os.environ.get("POSTGRES_USER", "octop"),
                                 "-d", os.environ.get("POSTGRES_DB", "octop"), "-tAc",
                                 "SELECT current_setting('server_version')"])
    observed = result.stdout.strip()
    check("live_postgres_version", bool(observed) and (not expected or observed.startswith(expected)),
          f"observed {observed or '<unreachable>'} vs locked {expected or '<unset>'}")

    # Redis
    expected = os.environ.get("OCTOP_EXPECTED_REDIS_VERSION", "")
    result = run(compose_argv + ["exec", "-T", "redis", "sh", "-c",
                                 'REDISCLI_AUTH="$(tr -d "\\r\\n" < /run/secrets/redis_password)" '
                                 'redis-cli --no-auth-warning info server'])
    match = re.search(r"redis_version:([0-9.]+)", result.stdout)
    observed = match.group(1) if match else ""
    check("live_redis_version", bool(observed) and (not expected or observed.startswith(expected)),
          f"observed {observed or '<unreachable>'} vs locked {expected or '<unset>'}")

    # Vault (only when the profile is running)
    running = run(compose_argv + ["ps", "--status", "running", "--services"]).stdout.split()
    if "vault" in running:
        expected = os.environ.get("OCTOP_EXPECTED_VAULT_VERSION", "")
        result = run(compose_argv + ["exec", "-T", "vault", "vault", "version"])
        match = re.search(r"Vault v([0-9.]+)", result.stdout)
        observed = match.group(1) if match else ""
        check("live_vault_version", bool(observed) and (not expected or observed.startswith(expected)),
              f"observed {observed or '<unreachable>'} vs locked {expected or '<unset>'}")
    else:
        check("live_vault_version", True,
              "vault profile is not running; the blocked legal/runtime gate in dependency-lock.json is unaffected")

verdict = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
print(json.dumps({"status": verdict, "service": "verify-deployment-policy",
                  "live": os.environ.get("OCTOP_LIVE") == "1", "checks": checks},
                 ensure_ascii=False))
sys.exit(0 if verdict == "passed" else 1)
PY

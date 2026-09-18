"""Fail-closed WorkBuddy dependency probes with secret-free evidence."""

from __future__ import annotations

import importlib.metadata
import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

from octop.infra.workbuddy.cel_sandbox import CELSandboxError, evaluate_cel

DependencyStatus = Literal["passed", "blocked", "failed"]
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    name: str
    status: DependencyStatus
    detail: str
    observed_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _package_check(distribution: str, expected: str) -> DependencyCheck:
    try:
        observed = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return DependencyCheck(distribution, "failed", "locked package is not installed")
    if observed != expected:
        return DependencyCheck(
            distribution,
            "failed",
            f"installed version does not match the WorkBuddy lock ({expected})",
            observed,
        )
    return DependencyCheck(distribution, "passed", "installed version matches lock", observed)


def _cel_check() -> DependencyCheck:
    try:
        result = evaluate_cel(
            "has(outputs.value) ? outputs.value : inputs.value",
            {"inputs": {"value": 7}, "outputs": {}},
        )
    except CELSandboxError as exc:
        return DependencyCheck("cel", "failed", f"{exc.code}: {exc.message}")
    if result.value != 7 or result.stats.runner != "InterpretedRunner":
        return DependencyCheck(
            "cel", "failed", "interpreter-only CEL smoke returned an invalid result"
        )
    memory = "enforced" if result.stats.memory_limit_enforced else "not enforceable on this OS"
    return DependencyCheck(
        "cel",
        "passed",
        f"lazy evaluation and has() passed; process memory limit {memory}",
        "cel-python 0.5.0",
    )


def _postgres_check(environ: dict[str, str]) -> DependencyCheck:
    dsn = environ.get("DATABASE_URL")
    if not dsn:
        return DependencyCheck(
            "postgresql",
            "blocked",
            "DATABASE_URL is not configured; PostgreSQL and pgvector runtime PoC did not run",
        )
    try:
        import psycopg

        with (
            psycopg.connect(dsn, connect_timeout=3) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SHOW server_version")
            version_row = cursor.fetchone()
            if version_row is None:
                return DependencyCheck(
                    "postgresql", "failed", "server version query returned no row"
                )
            server_version = str(version_row[0])
            cursor.execute(
                "SELECT installed_version FROM pg_available_extensions WHERE name = 'vector'"
            )
            row = cursor.fetchone()
    except Exception as exc:  # dependency boundary; return a controlled, secret-free result
        return DependencyCheck(
            "postgresql", "failed", f"connection or capability check failed: {type(exc).__name__}"
        )
    if row is None or row[0] is None:
        return DependencyCheck(
            "postgresql",
            "failed",
            "PostgreSQL is reachable but the pgvector extension is not installed",
            server_version,
        )
    return DependencyCheck(
        "postgresql",
        "passed",
        f"connection passed; pgvector {row[0]} installed",
        server_version,
    )


def _redis_check(environ: dict[str, str]) -> DependencyCheck:
    url = environ.get("REDIS_URL")
    if not url:
        return DependencyCheck(
            "redis",
            "blocked",
            "REDIS_URL is not configured; broker and rate-limit runtime PoC did not run",
        )
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        try:
            if not client.ping():
                return DependencyCheck("redis", "failed", "PING returned false")
            info = client.info(section="server")
        finally:
            client.close()
    except Exception as exc:
        return DependencyCheck("redis", "failed", f"connection check failed: {type(exc).__name__}")
    return DependencyCheck(
        "redis",
        "passed",
        "authenticated PING passed",
        str(info.get("redis_version", "unknown")),
    )


def _object_storage_check(environ: dict[str, str]) -> DependencyCheck:
    endpoint = environ.get("OBJECT_STORAGE_ENDPOINT")
    bucket = environ.get("OBJECT_STORAGE_BUCKET")
    if not endpoint or not bucket:
        return DependencyCheck(
            "object_storage",
            "blocked",
            "OBJECT_STORAGE_ENDPOINT and OBJECT_STORAGE_BUCKET are required; S3 runtime PoC did not run",
        )
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=environ.get("AWS_REGION"),
            config=Config(connect_timeout=3, read_timeout=3, retries={"max_attempts": 0}),
        )
        client.head_bucket(Bucket=bucket)
    except Exception as exc:
        return DependencyCheck(
            "object_storage",
            "failed",
            f"private bucket capability check failed: {type(exc).__name__}",
        )
    return DependencyCheck("object_storage", "passed", "private bucket HEAD capability passed")


def _vault_check(environ: dict[str, str]) -> DependencyCheck:
    address = environ.get("VAULT_ADDR")
    if not address:
        return DependencyCheck(
            "vault",
            "blocked",
            "VAULT_ADDR is not configured; health and workload-identity PoC did not run",
        )
    try:
        import httpx

        response = httpx.get(
            f"{address.rstrip('/')}/v1/sys/health",
            timeout=3,
            follow_redirects=False,
        )
    except Exception as exc:
        return DependencyCheck("vault", "failed", f"TLS health check failed: {type(exc).__name__}")
    if response.status_code not in {200, 429, 472, 473, 501, 503}:
        return DependencyCheck(
            "vault",
            "failed",
            f"health endpoint returned unexpected HTTP {response.status_code}",
        )
    return DependencyCheck(
        "vault",
        "blocked",
        "health endpoint is reachable; workload identity, secret read, rotation, and audit PoCs remain required",
    )


def _embedding_check(environ: dict[str, str]) -> DependencyCheck:
    configured = environ.get("EMBEDDING_MODEL_REVISION")
    if not configured:
        return DependencyCheck(
            "bge_m3",
            "blocked",
            "EMBEDDING_MODEL_REVISION is not configured; indexing and retrieval must remain disabled",
            BGE_M3_REVISION,
        )
    if configured != BGE_M3_REVISION:
        return DependencyCheck(
            "bge_m3",
            "failed",
            "configured revision does not match the locked BAAI/bge-m3 revision",
            configured,
        )
    return DependencyCheck(
        "bge_m3",
        "blocked",
        "revision matches lock; 1024-dimensional finite-vector and labelled-quality PoCs remain required",
        configured,
    )


def probe_dependencies(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Probe local packages and configured external services without exposing secrets."""

    values = dict(os.environ if environ is None else environ)
    checks = [
        _package_check("octop", "1.0.0"),
        _package_check("orcakit-harness-agent", "1.0.9"),
        _package_check("harness-memory", "0.9.10"),
        _package_check("harness-gateway", "0.9.7"),
        _package_check("harness-browser", "0.7.8"),
        _cel_check(),
        _postgres_check(values),
        _redis_check(values),
        _object_storage_check(values),
        _vault_check(values),
        _embedding_check(values),
    ]
    statuses = {check.status for check in checks}
    overall: DependencyStatus = (
        "failed" if "failed" in statuses else "blocked" if "blocked" in statuses else "passed"
    )
    return {
        "status": overall,
        "checks": [check.to_dict() for check in checks],
        "policy": {
            "silent_fallback": False,
            "secrets_in_output": False,
        },
    }

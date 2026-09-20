"""WorkBuddy lifecycle API contract: routing, auth, status codes and fail-closed errors.

The DB behaviour of the same endpoints is covered by
``test_workbuddy_lifecycle_postgresql.py``; here the service layer is replaced so
the router's own contract (tenant scoping, admin gating, envelopes, status
codes, ``no-store``) is asserted in isolation.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.routers import workbuddy_identity, workbuddy_lifecycle
from octop.api.routers.workbuddy_lifecycle import _require_recent_auth as real_recent_auth
from octop.api.routers.workbuddy_lifecycle import router
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy import lifecycle as policy


class _Principal:
    def __init__(self, tenant_id: str, user_id: int = 7, username: str = "wb-owner") -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.username = username
        self.user = SimpleNamespace(id=user_id, username=username)


def _dependency() -> Any:
    async def _inner() -> None:  # pragma: no cover - replaced per test
        raise AssertionError("dependency override was not installed")

    return _inner


def _app(
    principal: _Principal,
    *,
    admin_denied: bool = False,
    server: Any = None,
    routers: tuple[Any, ...] = (router,),
) -> FastAPI:
    app = FastAPI()
    for module in routers:
        app.include_router(module, prefix="/api/v1")

    @app.exception_handler(OctopError)
    async def _octop(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    stub = SimpleNamespace() if server is None else server
    overrides: dict[Any, Any] = {}
    for module in routers:
        for route in module.routes:
            for dependency in route.dependant.dependencies:  # type: ignore[attr-defined]
                call = dependency.call

                async def _resolve(_call: Any = call) -> Any:  # pragma: no cover - signature match
                    raise AssertionError("unused")

                if getattr(call, "__module__", "") == workbuddy_identity.__name__:

                    def _principal_dep(
                        _denied: bool = admin_denied, _principal: Any = principal
                    ) -> Any:
                        async def _resolve_principal() -> Any:
                            if _denied:
                                raise OctopError(
                                    ErrorCode.FORBIDDEN, "workbuddy admin role required"
                                )
                            return _principal

                        return _resolve_principal

                    overrides[call] = _principal_dep()
                elif (
                    getattr(call, "__module__", "") == "octop.api.deps"
                    and getattr(call, "__name__", "") == "get_server"
                ):
                    overrides[call] = lambda: stub
    app.dependency_overrides.update(overrides)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def principal() -> _Principal:
    return _Principal(tenant_id=str(uuid.uuid4()))


@pytest.fixture(autouse=True)
def _stub_repo_and_recent_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workbuddy_lifecycle, "_repo", lambda server: SimpleNamespace())
    monkeypatch.setattr(workbuddy_lifecycle, "_require_recent_auth", lambda request, server: None)


async def test_export_returns_job_and_single_redeem_token(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = policy.ExportView(
        export_job_id=str(uuid.uuid4()),
        tenant_id=principal.tenant_id,
        status="ready",
        requested_by=principal.user_id,
        created_at=1_700_000_000,
        completed_at=1_700_000_000,
        redeem_expires_at=1_700_000_000 + policy.REDEEM_TTL_SECONDS,
        redeemed_at=None,
        failure_reason=None,
        table_total=2,
        row_total=5,
        manifest={"schema": policy.EXPORT_SCHEMA},
        manifest_sha256="a" * 64,
        content_sha256="b" * 64,
        version=2,
    )
    issue = policy.ExportIssue(
        job=job, redeem_token="raw-token-value", redeem_expires_at=job.redeem_expires_at
    )
    monkeypatch.setattr(policy, "start_tenant_export", lambda repo, **kwargs: issue)

    async with _client(_app(principal)) as client:
        response = await client.post(f"/api/v1/tenants/{principal.tenant_id}/export")

    assert response.status_code == 202
    body = response.json()
    assert body["data"]["job_id"] == job.export_job_id
    assert body["data"]["redeem_token"] == "raw-token-value"
    assert response.headers["cache-control"] == "no-store"


async def test_export_of_another_tenant_is_a_uniform_not_found(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("another tenant must never reach the service layer")

    monkeypatch.setattr(policy, "start_tenant_export", _explode)
    async with _client(_app(principal)) as client:
        response = await client.post(f"/api/v1/tenants/{uuid.uuid4()}/export")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


async def test_export_requires_a_tenant_admin(principal: _Principal) -> None:
    async with _client(_app(principal, admin_denied=True)) as client:
        response = await client.post(f"/api/v1/tenants/{principal.tenant_id}/export")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value


async def test_deletion_fails_closed_without_a_compliance_policy(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _gate(repo: Any, **kwargs: Any) -> Any:
        raise OctopError(ErrorCode.COMPLIANCE_GATE_CLOSED, "no signed compliance policy")

    monkeypatch.setattr(policy, "request_tenant_deletion", _gate)
    async with _client(_app(principal)) as client:
        response = await client.post(f"/api/v1/tenants/{principal.tenant_id}/deletion-requests")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.COMPLIANCE_GATE_CLOSED.value


async def test_cancel_outside_cooling_off_maps_to_409(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _closed(repo: Any, **kwargs: Any) -> Any:
        raise OctopError(ErrorCode.DELETION_CANCEL_WINDOW_CLOSED, "the window has closed")

    monkeypatch.setattr(policy, "cancel_tenant_deletion", _closed)
    request_id = str(uuid.uuid4())
    async with _client(_app(principal)) as client:
        response = await client.post(
            f"/api/v1/tenants/{principal.tenant_id}/deletion-requests/{request_id}/cancel",
            json={"expected_version": 3},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.DELETION_CANCEL_WINDOW_CLOSED.value


async def test_missing_tenant_reports_not_found(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(policy, "read_export_manifest", lambda repo, **kwargs: None)
    async with _client(_app(principal)) as client:
        response = await client.get(f"/api/v1/exports/{uuid.uuid4()}/manifest")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND.value


async def test_manifest_response_is_no_store(
    principal: _Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "export_job_id": str(uuid.uuid4()),
        "status": "ready",
        "manifest": {},
        "manifest_sha256": "a" * 64,
    }
    monkeypatch.setattr(policy, "read_export_manifest", lambda repo, **kwargs: payload)
    async with _client(_app(principal)) as client:
        response = await client.get(f"/api/v1/exports/{payload['export_job_id']}/manifest")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["manifest_sha256"] == "a" * 64


def test_recent_authentication_is_required_without_a_token() -> None:
    request = SimpleNamespace(headers={})
    with pytest.raises(OctopError) as failure:
        real_recent_auth(request, SimpleNamespace())
    assert failure.value.code is ErrorCode.PRECONDITION_REQUIRED


# ── stage D: re-authentication credential and export download challenge ─────
#
# The two routes form one chain: ``POST /auth/reauthenticate`` proves the
# password and mints a five-minute, one-time, purpose-bound credential;
# ``POST /exports/{id}/download-challenge`` spends it on the admin who requested
# the export and hands back the job's redeem challenge.  The service layer runs
# for real here, against in-memory primitives whose conditional UPDATEs mirror
# the SQL guards, so single-use behaviour is asserted and not simulated.

_PASSWORD = "correct-horse-battery"


class _UserManager:
    """Small user manager double: the right password returns the caller."""

    def __init__(self, *, username: str, user_id: int, password: str) -> None:
        self.username = username
        self.user_id = user_id
        self.password = password

    async def authenticate(self, username: str, password: str) -> Any:
        if username != self.username or password != self.password:
            return None
        return SimpleNamespace(id=self.user_id, username=username)


class _MemoryLifecycleRepo:
    """Lifecycle primitives backed by dicts, with the SQL guard semantics.

    Only the tables the export challenge and the redeem path touch are modelled,
    and every guard repeats the database condition verbatim
    (``consumed_at IS NULL AND expires_at > now`` for a consume), so the policy
    under test observes real single-use behaviour.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.credentials: list[dict[str, Any]] = []
        self.tokens: list[dict[str, Any]] = []
        self.ledger: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []

    @contextmanager
    def transaction(self, ctx: Any) -> Iterator[None]:
        yield None

    # ── seeding ─────────────────────────────────────────────────────────

    def add_export_job(
        self,
        *,
        tenant_id: str,
        requested_by: int | None,
        created_at: int,
        status: str = "ready",
        manifest: bool = True,
    ) -> dict[str, Any]:
        """One export job whose 72h window starts at ``created_at``."""
        job: dict[str, Any] = {
            "export_job_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "requested_by": requested_by,
            "status": status,
            "created_at": int(created_at),
            "completed_at": int(created_at),
            "redeem_expires_at": int(created_at) + policy.REDEEM_TTL_SECONDS,
            "redeemed_at": int(created_at) if status == "redeemed" else None,
            "version": 2,
            "manifest_json": json.dumps({"tables": []}) if manifest else None,
            "manifest_sha256": ("a" * 64) if manifest else None,
            "content_sha256": ("b" * 64) if manifest else None,
            "table_total": 0,
            "row_total": 0,
            "failure_reason": None,
        }
        self.jobs[job["export_job_id"]] = job
        return dict(job)

    # ── export jobs ─────────────────────────────────────────────────────

    def get_export_job(self, conn: Any, *, tenant_id: str, export_job_id: str) -> Any:
        job = self.jobs.get(str(export_job_id))
        if job is None or job["tenant_id"] != str(tenant_id):
            return None
        return dict(job)

    def list_export_artifacts(self, conn: Any, *, tenant_id: str, export_job_id: str) -> list[Any]:
        return [
            dict(item)
            for item in self.artifacts
            if item["tenant_id"] == str(tenant_id) and item["export_job_id"] == str(export_job_id)
        ]

    def mark_export_redeemed(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        redeemed_by: int | None,
        redeemed_at: int,
    ) -> bool:
        job = self.jobs.get(str(export_job_id))
        if job is None or job["tenant_id"] != str(tenant_id) or job["status"] != "ready":
            return False
        job["status"] = "redeemed"
        job["redeemed_at"] = int(redeemed_at)
        job["redeemed_by"] = redeemed_by
        return True

    # ── redeem tokens ───────────────────────────────────────────────────

    def revoke_live_redeem_tokens(
        self, conn: Any, *, tenant_id: str, export_job_id: str, revoked_at: int
    ) -> int:
        revoked = 0
        for token in self.tokens:
            live = token["consumed_at"] is None and token["revoked_at"] is None
            if (
                live
                and token["tenant_id"] == str(tenant_id)
                and token["export_job_id"] == str(export_job_id)
            ):
                token["revoked_at"] = int(revoked_at)
                revoked += 1
        return revoked

    def insert_redeem_token(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        token_sha256: str,
        issued_by: int | None,
        issued_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        record = {
            "redeem_token_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "export_job_id": str(export_job_id),
            "token_sha256": token_sha256,
            "issued_by": issued_by,
            "issued_at": int(issued_at),
            "expires_at": int(expires_at),
            "revoked_at": None,
            "consumed_at": None,
            "consumed_by": None,
        }
        self.tokens.append(record)
        return dict(record)

    def get_redeem_token(self, conn: Any, *, tenant_id: str, token_sha256: str) -> Any:
        for token in self.tokens:
            if token["tenant_id"] == str(tenant_id) and token["token_sha256"] == token_sha256:
                return dict(token)
        return None

    def consume_redeem_token(
        self,
        conn: Any,
        *,
        tenant_id: str,
        token_sha256: str,
        consumed_by: int | None,
        consumed_at: int,
    ) -> bool:
        for token in self.tokens:
            if (
                token["tenant_id"] == str(tenant_id)
                and token["token_sha256"] == token_sha256
                and token["consumed_at"] is None
                and token["revoked_at"] is None
                and token["expires_at"] > int(consumed_at)
            ):
                token["consumed_at"] = int(consumed_at)
                token["consumed_by"] = consumed_by
                return True
        return False

    # ── re-authentication credentials ───────────────────────────────────

    def insert_reauth_credential(
        self,
        conn: Any,
        *,
        tenant_id: str,
        user_id: int,
        purpose: str,
        credential_sha256: str,
        issued_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        record = {
            "reauth_credential_id": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "user_id": int(user_id),
            "purpose": str(purpose),
            "credential_sha256": credential_sha256,
            "issued_at": int(issued_at),
            "expires_at": int(expires_at),
            "consumed_at": None,
        }
        self.credentials.append(record)
        return dict(record)

    def get_reauth_credential(self, conn: Any, *, tenant_id: str, credential_sha256: str) -> Any:
        for record in self.credentials:
            if (
                record["tenant_id"] == str(tenant_id)
                and record["credential_sha256"] == credential_sha256
            ):
                return dict(record)
        return None

    def consume_reauth_credential(
        self, conn: Any, *, tenant_id: str, credential_sha256: str, consumed_at: int
    ) -> bool:
        for record in self.credentials:
            if (
                record["tenant_id"] == str(tenant_id)
                and record["credential_sha256"] == credential_sha256
                and record["consumed_at"] is None
                and record["expires_at"] > int(consumed_at)
            ):
                record["consumed_at"] = int(consumed_at)
                return True
        return False

    # ── deletion ledger ─────────────────────────────────────────────────

    def ledger_head(self, conn: Any, *, tenant_id: str) -> Any:
        rows = [row for row in self.ledger if row["tenant_id"] == str(tenant_id)]
        return dict(rows[-1]) if rows else None

    def insert_ledger_entry(self, conn: Any, **kwargs: Any) -> dict[str, Any]:
        record = {**kwargs, "ledger_entry_id": str(uuid.uuid4())}
        self.ledger.append(record)
        return dict(record)


def _stage_d_app(
    principal: _Principal, *, password: str = _PASSWORD, admin_denied: bool = False
) -> FastAPI:
    """Both stage-D routers, a password-checking server double and the seams."""
    server = SimpleNamespace(
        user_manager=_UserManager(
            username=principal.username, user_id=principal.user_id, password=password
        )
    )
    return _app(
        principal,
        admin_denied=admin_denied,
        server=server,
        routers=(workbuddy_identity.router, router),
    )


async def _mint_credential(
    client: httpx.AsyncClient, *, password: str = _PASSWORD, purpose: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"password": password}
    if purpose is not None:
        body["purpose"] = purpose
    response = await client.post("/api/v1/auth/reauthenticate", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.fixture
def stage_d(monkeypatch: pytest.MonkeyPatch) -> _MemoryLifecycleRepo:
    repo = _MemoryLifecycleRepo()
    monkeypatch.setattr(workbuddy_lifecycle, "_repo", lambda server: repo)
    monkeypatch.setattr(workbuddy_identity, "_credential_repo", lambda server: repo)
    return repo


def _challenge_url(export_job_id: str) -> str:
    return f"/api/v1/exports/{export_job_id}/download-challenge"


async def test_reauthenticate_issues_a_no_store_five_minute_credential(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    async with _client(_stage_d_app(principal)) as client:
        response = await client.post("/api/v1/auth/reauthenticate", json={"password": _PASSWORD})

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    issued = response.json()["data"]
    assert issued["purpose"] == "export-download"
    assert policy.REAUTH_TTL_SECONDS == 300
    assert 295 <= issued["expires_at"] - int(time.time()) <= policy.REAUTH_TTL_SECONDS
    # Only the digest is ever stored: the raw credential exists in this response.
    stored = stage_d.credentials[0]
    assert stored["credential_sha256"] == policy.hash_redeem_token(issued["credential"])
    assert issued["credential"] not in json.dumps(stored)


async def test_reauthenticate_refuses_a_wrong_password_without_leaking(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    async with _client(_stage_d_app(principal)) as client:
        response = await client.post(
            "/api/v1/auth/reauthenticate", json={"password": "not-my-password"}
        )

    assert response.status_code == 401
    failure = response.json()["error"]
    assert failure["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value
    assert failure["message"] == "invalid credentials"
    assert failure["details"] == {}
    assert stage_d.credentials == []


async def test_download_challenge_is_the_redeem_token_and_keeps_the_72h_window(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    now = int(time.time())
    job = stage_d.add_export_job(
        tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=now
    )
    async with _client(_stage_d_app(principal)) as client:
        credential = await _mint_credential(client)
        response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": credential["credential"]}
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    challenge = response.json()["data"]
    # The challenge carries the window frozen when the export was created: the
    # stored job, and therefore the 72h it started with, is untouched.
    assert challenge["export_job_id"] == job["export_job_id"]
    assert challenge["expires_at"] == job["created_at"] + policy.REDEEM_TTL_SECONDS
    assert stage_d.jobs[job["export_job_id"]]["redeem_expires_at"] == challenge["expires_at"]

    # It is the redeem token of the existing one-time mechanism, not a new one:
    # the first redeem wins and the second is refused.
    download = policy.redeem_export(
        stage_d,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        token=challenge["challenge"],
    )
    assert download.export_job_id == job["export_job_id"]
    with pytest.raises(OctopError) as replay:
        policy.redeem_export(
            stage_d,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            token=challenge["challenge"],
        )
    assert replay.value.code is ErrorCode.EXPORT_REDEEM_CONSUMED


async def test_download_challenge_credential_is_single_use(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    job = stage_d.add_export_job(
        tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=int(time.time())
    )
    async with _client(_stage_d_app(principal)) as client:
        credential = await _mint_credential(client)
        first = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": credential["credential"]}
        )
        second = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": credential["credential"]}
        )

    assert first.status_code == 200
    assert second.status_code == 401
    assert second.json()["error"]["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value
    # Exactly one challenge was minted for the export.
    assert len(stage_d.tokens) == 1
    assert second.json()["error"]["details"] == {}


async def test_download_challenge_refuses_expired_and_foreign_credentials(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    now = int(time.time())
    job = stage_d.add_export_job(
        tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=now
    )
    expired = policy.issue_reauth_credential(
        stage_d,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        now=now - policy.REAUTH_TTL_SECONDS - 1,
    )
    other_purpose = policy.issue_reauth_credential(
        stage_d,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        purpose="deletion-cancel",
    )
    other_user = policy.issue_reauth_credential(
        stage_d, tenant_id=principal.tenant_id, user_id=principal.user_id + 1
    )
    other_tenant = policy.issue_reauth_credential(
        stage_d, tenant_id=str(uuid.uuid4()), user_id=principal.user_id
    )
    async with _client(_stage_d_app(principal)) as client:
        expired_response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": expired.credential}
        )
        purpose_response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": other_purpose.credential}
        )
        user_response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": other_user.credential}
        )
        tenant_response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": other_tenant.credential}
        )
        unknown_response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": "unknown-" + "x" * 32}
        )

    assert expired_response.status_code == 410
    assert expired_response.json()["error"]["code"] == ErrorCode.DOWNLOAD_GRANT_EXPIRED.value
    for refused in (purpose_response, user_response, tenant_response, unknown_response):
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == ErrorCode.AUTH_INVALID_CREDENTIALS.value
    assert stage_d.tokens == []
    assert [row["consumed_at"] for row in stage_d.credentials] == [None] * 4


async def test_download_challenge_refuses_other_exports_and_a_missing_credential(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    now = int(time.time())
    foreign = stage_d.add_export_job(
        tenant_id=principal.tenant_id, requested_by=principal.user_id + 1, created_at=now
    )
    async with _client(_stage_d_app(principal)) as client:
        credential = await _mint_credential(client)
        refused = await client.post(
            _challenge_url(foreign["export_job_id"]), json={"credential": credential["credential"]}
        )
        assert refused.status_code == 403
        assert refused.json()["error"]["code"] == ErrorCode.FORBIDDEN_RESOURCE_ACTION.value
        missing = await client.post(_challenge_url(str(uuid.uuid4())), json={})
        assert missing.status_code == 422
        unknown = await client.post(
            _challenge_url(str(uuid.uuid4())), json={"credential": credential["credential"]}
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == ErrorCode.EXPORT_JOB_NOT_FOUND.value
        malformed = await client.post(
            _challenge_url("not-a-uuid"), json={"credential": credential["credential"]}
        )
        assert malformed.status_code == 404
        assert malformed.json()["error"]["code"] == ErrorCode.NOT_FOUND.value
        # Nothing was minted and nothing was spent: the same credential still
        # works for the export this admin actually requested.
        assert stage_d.tokens == []
        mine = stage_d.add_export_job(
            tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=now
        )
        granted = await client.post(
            _challenge_url(mine["export_job_id"]), json={"credential": credential["credential"]}
        )

    assert granted.status_code == 200
    assert len(stage_d.tokens) == 1


async def test_download_challenge_requires_a_tenant_admin(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    job = stage_d.add_export_job(
        tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=int(time.time())
    )
    async with _client(_stage_d_app(principal, admin_denied=True)) as client:
        response = await client.post(
            _challenge_url(job["export_job_id"]), json={"credential": "x" * 32}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.FORBIDDEN.value
    assert stage_d.tokens == []


async def test_download_challenge_never_extends_a_closed_or_redeemed_export(
    principal: _Principal, stage_d: _MemoryLifecycleRepo
) -> None:
    now = int(time.time())
    stale = stage_d.add_export_job(
        tenant_id=principal.tenant_id,
        requested_by=principal.user_id,
        created_at=now - policy.REDEEM_TTL_SECONDS - 60,
    )
    redeemed = stage_d.add_export_job(
        tenant_id=principal.tenant_id,
        requested_by=principal.user_id,
        created_at=now,
        status="redeemed",
    )
    async with _client(_stage_d_app(principal)) as client:
        credential = await _mint_credential(client)
        closed = await client.post(
            _challenge_url(stale["export_job_id"]), json={"credential": credential["credential"]}
        )
        assert closed.status_code == 410
        assert closed.json()["error"]["code"] == ErrorCode.EXPORT_REDEEM_EXPIRED.value
        assert stage_d.jobs[stale["export_job_id"]]["redeem_expires_at"] < now
        consumed = await client.post(
            _challenge_url(redeemed["export_job_id"]), json={"credential": credential["credential"]}
        )
        assert consumed.status_code == 409
        assert consumed.json()["error"]["code"] == ErrorCode.EXPORT_REDEEM_CONSUMED.value
        assert stage_d.tokens == []
        # A refusal that never minted a challenge leaves the credential live.
        fresh = stage_d.add_export_job(
            tenant_id=principal.tenant_id, requested_by=principal.user_id, created_at=now
        )
        granted = await client.post(
            _challenge_url(fresh["export_job_id"]), json={"credential": credential["credential"]}
        )

    assert granted.status_code == 200
    assert granted.json()["data"]["expires_at"] == fresh["created_at"] + policy.REDEEM_TTL_SECONDS
    assert len(stage_d.tokens) == 1

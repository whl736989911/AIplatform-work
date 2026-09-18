"""WorkBuddy lifecycle API contract: routing, auth, status codes and fail-closed errors.

The DB behaviour of the same endpoints is covered by
``test_workbuddy_lifecycle_postgresql.py``; here the service layer is replaced so
the router's own contract (tenant scoping, admin gating, envelopes, status
codes, ``no-store``) is asserted in isolation.
"""

from __future__ import annotations

import uuid
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
    def __init__(self, tenant_id: str, user_id: int = 7) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.user = SimpleNamespace(id=user_id)


def _dependency() -> Any:
    async def _inner() -> None:  # pragma: no cover - replaced per test
        raise AssertionError("dependency override was not installed")

    return _inner


def _app(principal: _Principal, *, admin_denied: bool = False) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    @app.exception_handler(OctopError)
    async def _octop(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    overrides: dict[Any, Any] = {}
    for route in router.routes:
        for dependency in route.dependant.dependencies:  # type: ignore[attr-defined]
            call = dependency.call

            async def _resolve(_call: Any = call) -> Any:  # pragma: no cover - signature match
                raise AssertionError("unused")

            if getattr(call, "__module__", "") == workbuddy_identity.__name__:

                def _principal_dep(_denied: bool = admin_denied, _principal: Any = principal) -> Any:
                    async def _resolve_principal() -> Any:
                        if _denied:
                            raise OctopError(ErrorCode.FORBIDDEN, "workbuddy admin role required")
                        return _principal

                    return _resolve_principal

                overrides[call] = _principal_dep()
            elif getattr(call, "__module__", "") == "octop.api.deps" and getattr(
                call, "__name__", ""
            ) == "get_server":
                overrides[call] = lambda: SimpleNamespace()
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


async def test_manifest_response_is_no_store(principal: _Principal, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"export_job_id": str(uuid.uuid4()), "status": "ready", "manifest": {}, "manifest_sha256": "a" * 64}
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

"""WorkBuddy ``/api/v1`` must fail closed on a non-PostgreSQL control plane.

A2 tenant isolation rests on PostgreSQL row level security plus transaction-local
``app.*`` settings, so a SQLite control plane must never serve tenant data. These
checks pin the boundary end to end: the routers are mounted at ``/api/v1``,
unauthenticated calls are rejected before any handler runs, public WorkBuddy
entry points bypass the JWT gate but still fail closed, and the legacy ``/api``
surface keeps working.
"""

from __future__ import annotations

import pytest

from octop.infra.errors import ErrorCode
from tests.support.auth import bootstrap_admin

pytestmark = pytest.mark.asyncio

_POSTGRES_REQUIRED = ErrorCode.WORKBUDDY_POSTGRES_REQUIRED.value


@pytest.fixture
async def client(app_client):
    yield app_client


@pytest.fixture
async def admin_client(app_client):
    """An install with one admin, plus that admin's bearer header."""
    c, _, home = app_client
    await bootstrap_admin(c, home, username="alice", password="TestPass12")
    r = await c.post("/api/auth/login", json={"username": "alice", "password": "TestPass12"})
    assert r.status_code == 200
    yield c, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_tenant_routes_fail_closed_without_postgres(admin_client):
    """Tenant reads must answer the controlled 503, never touch a tenant table."""
    c, auth = admin_client

    for path in ("/api/v1/tenant-context", "/api/v1/users", "/api/v1/departments"):
        r = await c.get(path, headers=auth)
        assert r.status_code == 503, path
        assert r.json()["error"]["code"] == _POSTGRES_REQUIRED, path


async def test_catalog_routes_fail_closed_without_postgres(admin_client):
    """Credential metadata must not be served, listed, or created on SQLite."""
    c, auth = admin_client

    r = await c.get("/api/v1/connector-credentials", headers=auth)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == _POSTGRES_REQUIRED

    r = await c.post(
        "/api/v1/connector-credentials",
        headers=auth,
        json={"connector_type": "mcp", "display_name": "acme", "secret": "sk-live-x"},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == _POSTGRES_REQUIRED


async def test_unauthenticated_workbuddy_calls_are_rejected(admin_client):
    """No token: the JWT gate answers before any WorkBuddy handler runs."""
    c, _ = admin_client
    for path in ("/api/v1/tenant-context", "/api/v1/users"):
        r = await c.get(path)
        assert r.status_code == 401, path


async def test_public_login_entry_point_bypasses_jwt_but_fails_closed(admin_client):
    """``/api/v1/auth/login`` is JWT-exempt; isolation still decides the outcome."""
    c, _ = admin_client
    r = await c.post(
        "/api/v1/auth/login",
        headers={"X-Tenant-Slug": "acme"},
        json={"username": "alice@example.test", "password": "TestPass12"},
    )
    assert r.status_code == 503
    assert r.json()["error"]["code"] == _POSTGRES_REQUIRED


async def test_setup_lockdown_still_covers_workbuddy(client):
    """An unconfigured install must not reach WorkBuddy handlers either."""
    c, _, _ = client
    r = await c.get("/api/v1/tenant-context")
    assert r.status_code == 503
    assert r.json() == {"setup_required": True}


async def test_legacy_surface_is_unaffected(admin_client):
    """The upstream ``/api`` surface keeps serving; only WorkBuddy is gated."""
    c, auth = admin_client
    assert (await c.get("/api/health")).status_code == 200
    assert (await c.get("/api/users", headers=auth)).status_code == 200

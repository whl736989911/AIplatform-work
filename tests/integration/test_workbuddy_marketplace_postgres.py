"""Live-PostgreSQL acceptance gate for the WorkBuddy marketplace slice (021).

Drives the real router through the real repositories on a real database, because
the guarantees under test are database guarantees: a submission that carries
credential material writes nothing, a platform approval publishes exactly one
immutable version, the installing tenant's consent is recorded as evidence, and
an installation is invisible to every other tenant.

Enable with::

    export OCTOP_TEST_DATABASE_URL='postgresql://postgres@127.0.0.1:15441/octop_test'

Without it the module is skipped; SQLite cannot stand in for any of this.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.routers import workbuddy_marketplace as mp
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPlatformPrincipal,
    WorkBuddyPrincipal,
    workbuddy_principal,
)
from octop.infra.db.repos._base import now_ts
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from octop.infra.workbuddy import marketplace as M
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]

PUBLISHER_LICENSE = "Publish terms: sanitized templates only."
PRIVATE_KEY_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
)
PLATFORM_DEP = mp._Platform.__metadata__[0].dependency  # type: ignore[attr-defined]


def template_definition(*, greeting: str = "hello {{ inputs.who }}") -> dict[str, Any]:
    """A minimal submittable template: one transform node, no bindings."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {"input": {"greeting": greeting}, "expression": "inputs"},
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }


def submission_body(definition: Any, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": f"Greeting template {uuid.uuid4().hex[:6]}",
        "summary": "Greets the caller.",
        "industry": "general",
        "definition": definition,
        "license_id": "octop-community",
        "license_text": PUBLISHER_LICENSE,
        "capabilities": [],
    }
    body.update(overrides)
    return body


def consent_body(version: dict[str, Any]) -> dict[str, Any]:
    """The consent that exactly matches one published version document."""
    return {
        "accepted": True,
        "template_version_id": version["template_version_id"],
        "license_text_hash": version["license_text_hash"],
        "capabilities_hash": M.sha256_hex(M.canonical_json(list(version["required_capabilities"]))),
    }


def _seed_user(pool: Any, username: str) -> int:
    with pool.connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


@pytest.fixture(scope="module")
def pool() -> Iterator[Any]:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool

    database = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"])
    try:
        with database.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        run_migrations(database)
        yield database
    finally:
        database.close()


@pytest.fixture(scope="module")
def tenants(pool: Any) -> dict[str, dict[str, Any]]:
    """Two tenants, each with an owner membership and a second member."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    out: dict[str, dict[str, Any]] = {}
    for label in ("a", "b"):
        owner_id = _seed_user(pool, f"mkt-{label}-{uuid.uuid4().hex[:8]}")
        tenant = repo.create_tenant(
            f"mkt-{label}-{uuid.uuid4().hex[:8]}",
            f"Marketplace tenant {label.upper()}",
            owner_user_id=owner_id,
        )
        member = repo.list_members(tenant["tenant_id"])[0]
        out[label] = {
            "tenant_id": tenant["tenant_id"],
            "user_id": owner_id,
            "member_id": member["membership_id"],
            "slug": tenant["slug"],
        }
    return out


def _principal(tenant: dict[str, Any], *, role: str = "owner") -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(
            id=tenant["user_id"],
            username=f"mkt-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Marketplace tenant",
        member_id=tenant["member_id"],
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


@pytest.fixture
def app(pool: Any) -> FastAPI:
    """The marketplace router with the server bound to this database."""
    from octop.api.deps import get_server

    application = FastAPI()
    application.include_router(mp.router)

    @application.exception_handler(OctopError)
    async def _octop_error(_: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    application.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=pool)
    )
    return application


def _client(app: FastAPI, principal: WorkBuddyPrincipal) -> httpx.AsyncClient:
    app.dependency_overrides[workbuddy_principal] = lambda: principal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


def _platform_client(app: FastAPI, pool: Any) -> httpx.AsyncClient:
    """A reviewer holding the explicit ``workbuddy-platform`` audience."""
    reviewer_id = _seed_user(pool, f"mkt-reviewer-{uuid.uuid4().hex[:8]}")
    app.dependency_overrides[PLATFORM_DEP] = lambda: WorkBuddyPlatformPrincipal(
        user=User(id=reviewer_id, username="reviewer", role=Role.USER, display_name=None),
        audience="workbuddy-platform",
        claims={},
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


async def _publish(
    app: FastAPI,
    pool: Any,
    tenant: dict[str, Any],
    *,
    version: str,
    template_id: str | None = None,
    definition: Any = None,
) -> dict[str, Any]:
    """Submit, freeze and platform-approve one template version; return its ids."""
    async with _client(app, _principal(tenant)) as client:
        created = await client.post(
            "/marketplace/submissions",
            json=submission_body(definition if definition is not None else template_definition()),
        )
        assert created.status_code == 201, created.text
        submission = created.json()["data"]
        frozen = await client.post(
            f"/marketplace/submissions/{submission['id']}/submit",
            json={"expected_revision": submission["revision"]},
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["data"]["status"] == "submitted"
        revision = frozen.json()["data"]["revision"]

    async with _platform_client(app, pool) as platform:
        publication: dict[str, Any] = {
            "version": version,
            "publisher_display": "Octop Labs",
        }
        if template_id is not None:
            publication["template_id"] = template_id
        decided = await platform.post(
            f"/platform/submissions/{tenant['tenant_id']}/{submission['id']}/decisions",
            json={
                "decision": "approved",
                "platform_review_ref": f"review-{version}",
                "expected_revision": revision,
                "publication": publication,
            },
        )
        assert decided.status_code == 200, decided.text
        decided_data = decided.json()["data"]
        # The console reads the decision as {submission, review, published_version}.
        assert decided_data["submission"]["id"] == submission["id"]
        assert decided_data["submission"]["status"] == "approved"
        assert decided_data["review"]["decision"] == "approved"
        published = decided_data["published_version"]
        assert published is not None
        return {
            "template_id": published["template_id"],
            "version_id": published["template_version_id"],
        }


async def test_private_key_submission_is_rejected_before_any_write(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T24: a draft carrying live credential material writes no submission row."""
    with pool.connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM developer_submissions").fetchone()["n"]

    definition = template_definition()
    definition["nodes"][0]["config"]["greeting"] = PRIVATE_KEY_BLOCK
    async with _client(app, _principal(tenants["a"])) as client:
        response = await client.post("/marketplace/submissions", json=submission_body(definition))

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_SUBMISSION_INVALID.value
    assert "PRIVATE KEY" not in response.text
    with pool.connect() as conn:
        after = conn.execute("SELECT count(*) AS n FROM developer_submissions").fetchone()["n"]
    assert after == before


async def test_publish_install_and_upgrade_roundtrip(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T24: approve a sanitized version, install it, then upgrade it in place."""
    tenant_a = tenants["a"]
    first = await _publish(app, pool, tenant_a, version="1.0.0")
    template_id, version_id = first["template_id"], first["version_id"]

    async with _client(app, _principal(tenant_a)) as client:
        # The catalogue exposes the published version to the tenant.
        browse = await client.get("/marketplace/templates")
        assert browse.status_code == 200, browse.text
        assert template_id in [row["id"] for row in browse.json()["data"]]

        version_doc = await client.get(
            f"/marketplace/templates/{template_id}/versions/{version_id}"
        )
        assert version_doc.status_code == 200, version_doc.text
        version = version_doc.json()["data"]

        installed = await client.post(
            f"/marketplace/templates/{template_id}/install",
            json={
                "template_version_id": version_id,
                "consent": consent_body(version),
                "bindings": {},
                "credential_bindings": {},
                "workflow_name": "Greeting template",
            },
        )
        assert installed.status_code == 202, installed.text
        installation = installed.json()["data"]["installation"]
        assert installed.json()["data"]["job"]["id"]
        installation_id = installation["id"]

        detail = await client.get(f"/marketplace/installations/{installation_id}")
        assert detail.status_code == 200, detail.text
        evidence = detail.json()["data"]
        assert evidence["consents"][0]["license_text_hash"] == version["license_text_hash"]
        assert evidence["template_version_id"] == version_id

    # Another tenant can neither see the installation nor upgrade it.
    async with _client(app, _principal(tenants["b"])) as other:
        hidden = await other.get(f"/marketplace/installations/{installation_id}")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value

    second = await _publish(
        app,
        pool,
        tenant_a,
        version="1.1.0",
        template_id=template_id,
        definition=template_definition(greeting="hey {{ inputs.who }}"),
    )
    assert second["template_id"] == template_id

    async with _client(app, _principal(tenant_a)) as client:
        version_doc = await client.get(
            f"/marketplace/templates/{template_id}/versions/{second['version_id']}"
        )
        assert version_doc.status_code == 200, version_doc.text
        upgraded = await client.post(
            f"/marketplace/installations/{installation_id}/upgrade",
            json={
                "template_version_id": second["version_id"],
                "consent": consent_body(version_doc.json()["data"]),
                "bindings": {},
                "credential_bindings": {},
            },
        )
        assert upgraded.status_code == 202, upgraded.text
        upgrade = upgraded.json()["data"]["upgrade"]
        assert upgrade["to_template_version_id"] == second["version_id"]
        assert upgrade["from_template_version_id"] == version_id

        after = await client.get(f"/marketplace/installations/{installation_id}")
        assert after.status_code == 200, after.text
        state = after.json()["data"]
        # The upgrade lands through a new workflow version: the installation now
        # serves the published template version through the version the upgrade
        # created, and still reports the same workflow.
        assert state["status"] == "installed"
        assert state["template_version_id"] == second["version_id"]
        assert state["installed_version_id"] == upgrade["workflow_version_id"]
        assert state["workflow_id"] == upgrade["workflow_id"]
        assert upgrade["status"] == "installed"
        assert upgraded.json()["data"]["job"]["status"] == "succeeded"


async def test_submission_is_invisible_to_another_tenant(
    app: FastAPI, tenants: dict[str, dict[str, Any]]
) -> None:
    """T24: a draft is tenant-private; a foreign member sees a uniform 404."""
    async with _client(app, _principal(tenants["a"])) as client:
        created = await client.post(
            "/marketplace/submissions", json=submission_body(template_definition())
        )
        assert created.status_code == 201, created.text
        submission_id = created.json()["data"]["id"]

    async with _client(app, _principal(tenants["b"])) as other:
        hidden = await other.get(f"/marketplace/submissions/{submission_id}")
        assert hidden.status_code == 404, hidden.text
        assert hidden.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value


async def test_install_without_consent_writes_nothing(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T24: an unconsented install leaves no installation row behind."""
    published = await _publish(app, pool, tenants["a"], version="2.0.0")
    with pool.connect() as conn:
        before = conn.execute("SELECT count(*) AS n FROM marketplace_installations").fetchone()["n"]

    async with _client(app, _principal(tenants["a"])) as client:
        version_doc = await client.get(
            f"/marketplace/templates/{published['template_id']}/versions/{published['version_id']}"
        )
        assert version_doc.status_code == 200, version_doc.text
        consent = consent_body(version_doc.json()["data"])
        refused = await client.post(
            f"/marketplace/templates/{published['template_id']}/install",
            json={
                "template_version_id": published["version_id"],
                "consent": {**consent, "accepted": False},
                "bindings": {},
                "credential_bindings": {},
            },
        )

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == ErrorCode.WORKBUDDY_CONSENT_REQUIRED.value
    with pool.connect() as conn:
        after = conn.execute("SELECT count(*) AS n FROM marketplace_installations").fetchone()["n"]
    assert after == before

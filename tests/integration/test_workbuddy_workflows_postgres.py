"""Live-PostgreSQL acceptance gate for the WorkBuddy workflow slice (017).

Drives the real router through the real repository on a real database, because
the guarantees under test are database guarantees: immutable versions, the
integer revision CAS behind ``If-Match``, candidate versions that no activation
path may publish, and tenant isolation on every one of those tables.

Enable with::

    export OCTOP_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:15441/octop_test'

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

from octop.api.routers import workbuddy_workflows
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal
from octop.infra.db.repos._base import now_ts
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]


def hello_definition() -> dict[str, Any]:
    """The minimal hello example: one transform node, no tools, no approvers."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {
                    "input": {"greeting": "hello {{ inputs.who }}"},
                    "expression": "inputs",
                },
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }


def cyclic_definition() -> dict[str, Any]:
    """Two nodes pointing at each other: must fail before persistence."""
    definition = hello_definition()
    definition["nodes"] = [
        {
            "id": "a",
            "type": "transform",
            "name": "A",
            "config": {"input": {}, "expression": "inputs"},
            "save_as": "out_a",
        },
        {
            "id": "b",
            "type": "transform",
            "name": "B",
            "config": {"input": {}, "expression": "inputs"},
            "save_as": "out_b",
        },
    ]
    definition["edges"] = [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]
    return definition


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
            # The upstream migrations declare a pgvector column, so the schema
            # reset above has to be followed by the deployment prerequisite.
            available = conn.execute(
                "SELECT 1 AS present FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
            if available is None:
                pytest.skip("pgvector is required by the upstream migrations")
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        run_migrations(database)
        yield database
    finally:
        database.close()


@pytest.fixture(scope="module")
def tenants(pool: Any) -> dict[str, dict[str, Any]]:
    """Two tenants, each with an owner membership."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    out: dict[str, dict[str, Any]] = {}
    for label in ("a", "b"):
        owner_id = _seed_user(pool, f"wf-{label}-{uuid.uuid4().hex[:8]}")
        tenant = repo.create_tenant(
            f"wf-{label}-{uuid.uuid4().hex[:8]}",
            f"Workflow tenant {label.upper()}",
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
            username=f"wf-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Workflow tenant",
        member_id=tenant["member_id"],
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


@pytest.fixture
def app(pool: Any) -> FastAPI:
    """The workflow router with the server bound to this database."""
    from octop.api.deps import get_server

    application = FastAPI()
    application.include_router(workbuddy_workflows.router)

    @application.exception_handler(OctopError)
    async def _octop_error(_: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    application.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=pool)
    )
    return application


def _client(app: FastAPI, principal: WorkBuddyPrincipal) -> httpx.AsyncClient:
    from octop.api.routers.workbuddy_identity import workbuddy_principal

    app.dependency_overrides[workbuddy_principal] = lambda: principal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


async def test_hello_workflow_roundtrip_and_version_cas(
    app: FastAPI, tenants: dict[str, dict[str, Any]]
) -> None:
    """T06/T23: create, save under If-Match, activate, roll back, list versions."""
    principal = _principal(tenants["a"])
    async with _client(app, principal) as client:
        created = await client.post(
            "/workflows", json={"name": "Hello WorkBuddy", "definition": hello_definition()}
        )
        assert created.status_code == 201, created.text
        body = created.json()["data"]
        workflow_id = body["id"]
        first_version = body["version"]["id"]
        etag = created.headers["etag"]
        assert body["revision"] == 1

        detail = await client.get(f"/workflows/{workflow_id}")
        assert detail.status_code == 200
        assert detail.headers["etag"] == etag

        # Missing If-Match is a precondition failure, not an implicit overwrite.
        missing = await client.put(
            f"/workflows/{workflow_id}",
            json={"definition": hello_definition(), "base_version_id": first_version},
        )
        assert missing.status_code == 428
        assert missing.json()["error"]["code"] == ErrorCode.PRECONDITION_REQUIRED.value

        stale = await client.put(
            f"/workflows/{workflow_id}",
            headers={"If-Match": '"deadbeef-0000-4000-8000-000000000000.99"'},
            json={"definition": hello_definition(), "base_version_id": first_version},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == ErrorCode.WF_VERSION_CONFLICT.value

        saved = await client.put(
            f"/workflows/{workflow_id}",
            headers={"If-Match": etag},
            json={
                "definition": hello_definition(),
                "base_version_id": first_version,
                "change_summary": "no semantic change",
            },
        )
        assert saved.status_code == 201, saved.text
        second_version = saved.json()["data"]["version"]["id"]
        second_etag = saved.headers["etag"]
        assert second_version != first_version
        assert second_etag != etag

        activated = await client.post(
            f"/workflows/{workflow_id}/activate",
            headers={"If-Match": second_etag},
            json={"version_id": second_version},
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["data"]["active_version_id"] == second_version

        versions = await client.get(f"/workflows/{workflow_id}/versions")
        assert versions.status_code == 200
        listed = versions.json()["data"]["items"]
        assert {item["id"] for item in listed} >= {first_version, second_version}

        rolled_back = await client.post(
            f"/workflows/{workflow_id}/rollback",
            headers={"If-Match": activated.headers["etag"]},
            json={"version_id": first_version},
        )
        assert rolled_back.status_code == 201, rolled_back.text
        rolled = rolled_back.json()["data"]["version"]
        assert rolled["id"] not in {first_version, second_version}
        assert rolled["origin"] == "rollback"
        assert rolled_back.json()["data"]["active_version_id"] == rolled["id"]


async def test_versions_are_immutable_in_the_database(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T06: a stored version can never be rewritten or deleted."""
    principal = _principal(tenants["a"])
    async with _client(app, principal) as client:
        created = await client.post(
            "/workflows", json={"name": "Immutable", "definition": hello_definition()}
        )
        assert created.status_code == 201, created.text
        version_id = created.json()["data"]["version"]["id"]

    # Each rejected statement aborts its transaction, so every attempt gets its own.
    for statement in (
        "UPDATE workbuddy_workflow_versions SET definition = '{}' WHERE workflow_version_id = ?",
        "DELETE FROM workbuddy_workflow_versions WHERE workflow_version_id = ?",
    ):
        with pytest.raises(Exception) as refused, pool.transaction() as conn:
            conn.execute(statement, (version_id,))
        assert refused.value is not None

    with pool.connect() as conn:
        survivors = conn.execute(
            "SELECT count(*) AS n FROM workbuddy_workflow_versions WHERE workflow_version_id = ?",
            (version_id,),
        ).fetchone()
    assert survivors["n"] == 1


async def test_candidate_versions_cannot_be_activated(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T23: a proposal candidate is not publishable through the activate route."""
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    principal = _principal(tenants["a"])
    async with _client(app, principal) as client:
        created = await client.post(
            "/workflows", json={"name": "Candidate", "definition": hello_definition()}
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["data"]["id"]
        revision = created.json()["data"]["revision"]

    compiled = compile_workflow_definition(hello_definition())
    repo = WorkBuddyWorkflowRepo(pool)
    candidate = repo.save_version(
        tenants["a"]["tenant_id"],
        workflow_id,
        definition=compiled.definition,
        definition_sha256=definition_sha256(compiled.definition),
        expected_revision=revision,
        created_by_user_id=principal.user.id,
        created_by_membership_id=principal.member_id,
        origin="proposal",
    )

    async with _client(app, principal) as client:
        detail = await client.get(f"/workflows/{workflow_id}")
        refused = await client.post(
            f"/workflows/{workflow_id}/activate",
            headers={"If-Match": detail.headers["etag"]},
            json={"version_id": candidate.version.workflow_version_id},
        )
    assert refused.status_code == 409, refused.text
    assert (
        refused.json()["error"]["code"]
        == ErrorCode.WORKBUDDY_WORKFLOW_VERSION_NOT_ACTIVATABLE.value
    )


async def test_member_listing_hides_drafts_and_withdrawn_workflows(
    app: FastAPI, pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T07/T08: a plain member sees only published, unrevoked workflows.

    The member branch of the listing route calls a repository helper the seeded
    branch never defined, so this path raised AttributeError until the
    integration test exercised it.
    """
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo

    owner = _principal(tenants["a"])
    tenant = tenants["a"]
    workflow_ids: dict[str, str] = {}
    version_ids: dict[str, str] = {}

    async with _client(app, owner) as client:
        for name in ("Published", "Draft only", "Withdrawn"):
            created = await client.post(
                "/workflows", json={"name": name, "definition": hello_definition()}
            )
            assert created.status_code == 201, created.text
            workflow_ids[name] = created.json()["data"]["id"]
            version_ids[name] = created.json()["data"]["version"]["id"]

        for name in ("Published", "Withdrawn"):
            detail = await client.get(f"/workflows/{workflow_ids[name]}")
            activated = await client.post(
                f"/workflows/{workflow_ids[name]}/activate",
                headers={"If-Match": detail.headers["etag"]},
                json={"version_id": version_ids[name]},
            )
            assert activated.status_code == 200, activated.text

    WorkBuddyWorkflowRepo(pool).revoke_version(
        tenant["tenant_id"],
        workflow_ids["Withdrawn"],
        None,
        reason="withdrawn for review",
        revoked_by_user_id=tenant["user_id"],
        revoked_by_membership_id=tenant["member_id"],
    )

    member = _principal(tenants["a"], role="member")
    async with _client(app, member) as client:
        listing = await client.get("/workflows")
        assert listing.status_code == 200, listing.text
        visible = [item["name"] for item in listing.json()["data"]["items"]]

    # Earlier tests in this module publish workflows in the same tenant, so the
    # contract is what a member must not see, not an exact list.
    assert "Published" in visible, visible
    assert "Draft only" not in visible, visible
    assert "Withdrawn" not in visible, visible


async def test_cross_tenant_workflows_are_invisible(
    app: FastAPI, tenants: dict[str, dict[str, Any]]
) -> None:
    """T02/T18: another tenant's workflow is a 404, not a forbidden resource."""
    owner = _principal(tenants["a"])
    async with _client(app, owner) as client:
        created = await client.post(
            "/workflows", json={"name": "Tenant A only", "definition": hello_definition()}
        )
        assert created.status_code == 201, created.text
        workflow_id = created.json()["data"]["id"]

    intruder = _principal(tenants["b"])
    async with _client(app, intruder) as client:
        detail = await client.get(f"/workflows/{workflow_id}")
        assert detail.status_code == 404
        listing = await client.get("/workflows")
        assert listing.status_code == 200
        assert listing.json()["data"]["items"] == []


async def test_invalid_definition_is_rejected_before_persistence(
    app: FastAPI, tenants: dict[str, dict[str, Any]]
) -> None:
    """T06/T08: a cyclic graph never reaches the database."""
    principal = _principal(tenants["a"])
    async with _client(app, principal) as client:
        validate = await client.post(
            "/workflow-definitions/validate", json={"definition": cyclic_definition()}
        )
        assert validate.status_code == 422
        assert validate.json()["error"]["code"] == ErrorCode.WF_INVALID_SCHEMA.value

        before = await client.get("/workflows")
        created = await client.post(
            "/workflows", json={"name": "Cycle", "definition": cyclic_definition()}
        )
        assert created.status_code == 422
        after = await client.get("/workflows")
        assert len(after.json()["data"]["items"]) == len(before.json()["data"]["items"])


async def test_concurrent_revision_cas_lets_exactly_one_writer_win(
    pool: Any, tenants: dict[str, dict[str, Any]]
) -> None:
    """T23: two writers holding the same revision cannot both append a version."""
    from octop.infra.db.repos.workbuddy_workflows import (
        RevisionConflict,
        WorkBuddyWorkflowRepo,
    )
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    repo = WorkBuddyWorkflowRepo(pool)
    compiled = compile_workflow_definition(hello_definition())
    digest = definition_sha256(compiled.definition)
    tenant = tenants["a"]

    bundle = repo.create_workflow(
        tenant["tenant_id"],
        name=f"CAS {uuid.uuid4().hex[:8]}",
        definition=compiled.definition,
        definition_sha256=digest,
        created_by_user_id=tenant["user_id"],
        created_by_membership_id=tenant["member_id"],
    )
    revision = bundle.workflow.revision

    repo.save_version(
        tenant["tenant_id"],
        bundle.workflow.workflow_id,
        definition=compiled.definition,
        definition_sha256=digest,
        expected_revision=revision,
        created_by_user_id=tenant["user_id"],
        created_by_membership_id=tenant["member_id"],
    )
    with pytest.raises(RevisionConflict):
        repo.save_version(
            tenant["tenant_id"],
            bundle.workflow.workflow_id,
            definition=compiled.definition,
            definition_sha256=digest,
            expected_revision=revision,
            created_by_user_id=tenant["user_id"],
            created_by_membership_id=tenant["member_id"],
        )

    versions = repo.list_versions(tenant["tenant_id"], bundle.workflow.workflow_id)
    assert [version.version_number for version in versions] == [2, 1]

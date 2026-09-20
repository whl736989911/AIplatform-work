"""Workflow visibility on PostgreSQL: the four permission layers decide who sees what.

Before this change every member of a tenant could list and read every workflow:
the list only filtered on publication state and the detail route asked nothing. The
permission model now answers both, and only a database can prove the parts that
matter here:

* a workflow is registered with its permission row in the same transaction that
  creates it, so the model can never hide the object it just created;
* the list query returns exactly the objects the pure resolver would allow for the
  same actor, including the explicit-grant arm and the department layer;
* a workflow with no permission row stays company visible (the state every workflow
  was in before the model existed) while management of it stays with its creator;
* a member holding ``write`` through a grant can manage the workflow, and one who
  holds nothing sees the same uniform 404 as a stranger.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``); the
database is dedicated to the suite and is reset here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from tests.support.postgresql import requires_postgresql

from octop.api.deps import get_server
from octop.api.routers import workbuddy_workflows
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal, workbuddy_principal
from octop.infra.db.migrate import (
    _ensure_workbuddy_visibility_backfill,
    run_migrations,
)
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_workflows import (
    RBAC_OBJECT_KIND,
    WorkBuddyWorkflowRepo,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.model import RbacActor
from octop.infra.rbac.service import RbacService
from octop.infra.users.identity import Role, User
from octop.infra.workbuddy.workflow_compiler import definition_sha256

pytestmark = [pytest.mark.postgresql, requires_postgresql]


def hello_definition(greeting: str = "hello {{ inputs.who }}") -> dict[str, Any]:
    """A minimal compilable workflow: one transform node, no tools or approvers."""
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


@pytest.fixture(scope="module")
def pool() -> Iterator[PostgresPool]:
    database = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"], min_size=1, max_size=2)
    with database.connect() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        available = conn.execute(
            "SELECT 1 AS present FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if available is None:
            pytest.skip("pgvector is required by the migrations under test")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    run_migrations(database)
    yield database
    database.close()


def _seed_user(pool: PostgresPool, username: str) -> int:
    with pool.connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


@pytest.fixture(scope="module")
def world(pool: PostgresPool) -> dict[str, Any]:
    """One tenant: owner, a department with a member, and a detached member."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"vis-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"vis-{uuid.uuid4().hex[:8]}", "Visibility tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member_id = str(identity.list_members(tenant_id)[0]["membership_id"])
    department = identity.create_department(tenant_id, name="Ops", actor_user_id=owner_id)
    department_id = str(department["department_id"])

    member_id = _seed_user(pool, f"vis-member-{uuid.uuid4().hex[:8]}")
    member_row = identity.add_membership(
        tenant_id, member_id, department_id=department_id, actor_user_id=owner_id
    )
    detached_id = _seed_user(pool, f"vis-detached-{uuid.uuid4().hex[:8]}")
    detached_row = identity.add_membership(tenant_id, detached_id, actor_user_id=owner_id)
    assert member_row is not None and detached_row is not None

    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "owner_member_id": owner_member_id,
        "department_id": department_id,
        "member_user_id": member_id,
        "department_member_user_id": member_id,
        "detached_user_id": detached_id,
        "membership_ids": {
            owner_id: owner_member_id,
            member_id: str(member_row["membership_id"]),
            detached_id: str(detached_row["membership_id"]),
        },
    }


def principal_for(
    world: dict[str, Any],
    user_id: int,
    *,
    role: str = "member",
    department_id: str | None = None,
) -> WorkBuddyPrincipal:
    """The caller as the router sees them; the membership id is the real UUID."""
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"vis-{user_id}", role=Role.USER, display_name=None),
        tenant_id=world["tenant_id"],
        tenant_slug="vis",
        tenant_name="Visibility tenant",
        member_id=world["membership_ids"][user_id],
        role=role,
        department_id=department_id,
        member_status="active",
        tenant_status="active",
    )


def actor_for(
    world: dict[str, Any],
    user_id: int,
    *,
    department_id: str | None = None,
    is_tenant_admin: bool = False,
) -> RbacActor:
    return RbacActor(
        user_id=user_id,
        tenant_id=world["tenant_id"],
        department_id=department_id,
        is_tenant_admin=is_tenant_admin,
    )


def context_for(world: dict[str, Any], user_id: int | None = None) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(
        world["tenant_id"], user_id=user_id if user_id is not None else world["owner_user_id"]
    )


@pytest.fixture
def app(pool: PostgresPool) -> FastAPI:
    application = FastAPI()
    application.include_router(workbuddy_workflows.router)

    @application.exception_handler(OctopError)
    async def _octop_error(_: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    application.dependency_overrides[get_server] = lambda: type(
        "Server", (), {"services": type("Services", (), {"db": pool})()}
    )()
    return application


def client_for(app: FastAPI, principal: WorkBuddyPrincipal) -> httpx.AsyncClient:
    app.dependency_overrides[workbuddy_principal] = lambda: principal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


async def create_workflow(
    app: FastAPI,
    principal: WorkBuddyPrincipal,
    *,
    greeting: str = "hello {{ inputs.who }}",
) -> dict[str, Any]:
    async with client_for(app, principal) as client:
        created = await client.post(
            "/workflows",
            json={"name": f"Flow {uuid.uuid4().hex[:6]}", "definition": hello_definition(greeting)},
        )
        assert created.status_code == 201, created.text
        return created.json()["data"]


def scope_rows(pool: PostgresPool, world: dict[str, Any]) -> dict[str, str]:
    """``workflow_id -> scope`` for the tenant, read straight from the table."""
    with pool.connect() as conn:
        rows = conn.execute(
            "SELECT object_id, scope FROM workbuddy_object_scopes "
            "WHERE tenant_id = ? AND object_kind = ?",
            (world["tenant_id"], RBAC_OBJECT_KIND),
        ).fetchall()
    return {str(row["object_id"]): str(row["scope"]) for row in rows}


async def listed_ids(app: FastAPI, principal: WorkBuddyPrincipal) -> set[str]:
    async with client_for(app, principal) as client:
        response = await client.get("/workflows")
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["data"]["items"]}


async def test_a_new_workflow_registers_its_permission_row(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The row lands with the workflow, or the model would hide what it just created."""
    created = await create_workflow(app, principal_for(world, world["owner_user_id"], role="owner"))
    assert scope_rows(pool, world)[created["id"]] == "enterprise"


async def test_the_member_list_follows_the_four_layers(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    owner = principal_for(world, world["owner_user_id"], role="owner")
    member = principal_for(
        world, world["department_member_user_id"], department_id=world["department_id"]
    )
    detached = principal_for(world, world["detached_user_id"])

    enterprise = await create_workflow(app, owner, greeting="enterprise")
    department = await create_workflow(app, owner, greeting="department")
    personal = await create_workflow(app, owner, greeting="personal")
    granted = await create_workflow(app, owner, greeting="granted")

    service = RbacService(pool)
    context = context_for(world)
    owner_actor = actor_for(world, world["owner_user_id"], is_tenant_admin=True)
    service.register_object(
        context,
        object_kind=RBAC_OBJECT_KIND,
        object_id=department["id"],
        scope="department",
        department_id=world["department_id"],
        actor=owner_actor,
    )
    service.register_object(
        context,
        object_kind=RBAC_OBJECT_KIND,
        object_id=personal["id"],
        scope="personal",
        owner_user_id=world["owner_user_id"],
        actor=owner_actor,
    )
    service.register_object(
        context,
        object_kind=RBAC_OBJECT_KIND,
        object_id=granted["id"],
        scope="personal",
        owner_user_id=world["owner_user_id"],
        actor=owner_actor,
    )
    service.grant(
        context,
        object_kind=RBAC_OBJECT_KIND,
        object_id=granted["id"],
        actor=owner_actor,
        permission="read",
        user_id=world["detached_user_id"],
    )

    # Every workflow is published before a member can see it in the list.
    for workflow in (enterprise, department, personal, granted):
        await publish(app, owner, workflow)

    owner_ids = await listed_ids(app, owner)
    member_ids = await listed_ids(app, member)
    detached_ids = await listed_ids(app, detached)

    assert {enterprise["id"], department["id"], personal["id"], granted["id"]} <= owner_ids
    assert enterprise["id"] in member_ids
    assert department["id"] in member_ids, "a department member reads the department layer"
    assert personal["id"] not in member_ids, "another member's personal workflow stays hidden"
    assert granted["id"] not in member_ids, "the grant is addressed to somebody else"
    assert enterprise["id"] in detached_ids
    assert department["id"] not in detached_ids, "a detached member is not in the department"
    assert personal["id"] not in detached_ids
    assert granted["id"] in detached_ids, "the explicit grant is the fourth layer"


async def publish(app: FastAPI, principal: WorkBuddyPrincipal, workflow: dict[str, Any]) -> None:
    """Activate the workflow so members see it in their execution catalogue."""
    workflow_id = workflow["id"]
    async with client_for(app, principal) as client:
        detail = await client.get(f"/workflows/{workflow_id}")
        assert detail.status_code == 200, detail.text
        etag = detail.headers["etag"]
        activated = await client.post(
            f"/workflows/{workflow_id}/activate",
            headers={"if-match": etag},
            json={"version_id": workflow["version"]["id"]},
        )
        assert activated.status_code == 200, activated.text


async def test_a_narrowed_workflow_is_a_uniform_404_for_other_members(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    owner = principal_for(world, world["owner_user_id"], role="owner")
    created = await create_workflow(app, owner)
    RbacService(pool).register_object(
        context_for(world),
        object_kind=RBAC_OBJECT_KIND,
        object_id=created["id"],
        scope="personal",
        owner_user_id=world["owner_user_id"],
        actor=actor_for(world, world["owner_user_id"], is_tenant_admin=True),
    )

    async with client_for(app, principal_for(world, world["detached_user_id"])) as client:
        hidden = await client.get(f"/workflows/{created['id']}")
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value

    async with client_for(app, owner) as client:
        visible = await client.get(f"/workflows/{created['id']}")
    assert visible.status_code == 200, visible.text


async def test_a_write_grant_lets_a_member_manage_but_not_a_stranger(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    owner = principal_for(world, world["owner_user_id"], role="owner")
    created = await create_workflow(app, owner)
    recipient = world["department_member_user_id"]

    service = RbacService(pool)
    service.grant(
        context_for(world),
        object_kind=RBAC_OBJECT_KIND,
        object_id=created["id"],
        actor=actor_for(world, world["owner_user_id"], is_tenant_admin=True),
        permission="write",
        user_id=recipient,
    )

    async with client_for(app, owner) as client:
        detail = await client.get(f"/workflows/{created['id']}")
        etag = detail.headers["etag"]
    first_version = created["version"]["id"]

    granted_client = client_for(
        app,
        principal_for(world, recipient, department_id=world["department_id"]),
    )
    async with granted_client as client:
        updated = await client.put(
            f"/workflows/{created['id']}",
            headers={"if-match": etag},
            json={"definition": hello_definition("hi there"), "base_version_id": first_version},
        )
    assert updated.status_code == 201, updated.text

    async with client_for(app, principal_for(world, world["detached_user_id"])) as client:
        refused = await client.put(
            f"/workflows/{created['id']}",
            headers={"if-match": etag},
            json={"definition": hello_definition("nope"), "base_version_id": first_version},
        )
    assert refused.status_code == 404, refused.text


async def test_a_workflow_without_a_permission_row_stays_company_visible(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    """Legacy rows keep working until the backfill in B-06 writes their scope."""
    owner = principal_for(world, world["owner_user_id"], role="owner")
    created = await create_workflow(app, owner)
    await publish(app, owner, created)

    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM workbuddy_object_scopes "
            "WHERE tenant_id = ? AND object_kind = ? AND object_id = ?",
            (world["tenant_id"], RBAC_OBJECT_KIND, created["id"]),
        )

    member = principal_for(world, world["detached_user_id"])
    assert created["id"] in await listed_ids(app, member)
    async with client_for(app, member) as client:
        readable = await client.get(f"/workflows/{created['id']}")
    assert readable.status_code == 200, readable.text

    # Management is unchanged: not the creator, not an admin, no grant → 404.
    async with client_for(app, member) as client:
        refused = await client.get(f"/workflows/{created['id']}/versions")
    assert refused.status_code == 404, refused.text


async def test_the_backfill_writes_the_implicit_row_and_stays_idempotent(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    """B-06: going live must not make pre-existing work disappear.

    The permission row is what the model reads; until the backfill writes it the
    object is only readable through the legacy fallback. Re-running the repair
    must neither duplicate the row nor move an existing one.
    """
    owner = principal_for(world, world["owner_user_id"], role="owner")
    created = await create_workflow(app, owner)
    await publish(app, owner, created)
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM workbuddy_object_scopes "
            "WHERE tenant_id = ? AND object_kind = ? AND object_id = ?",
            (world["tenant_id"], RBAC_OBJECT_KIND, created["id"]),
        )

    _ensure_workbuddy_visibility_backfill(pool)
    assert scope_rows(pool, world)[created["id"]] == "enterprise"

    before = scope_rows(pool, world)
    _ensure_workbuddy_visibility_backfill(pool)
    assert scope_rows(pool, world) == before

    member = principal_for(world, world["detached_user_id"])
    assert created["id"] in await listed_ids(app, member)


def test_the_resolver_and_the_list_agree_for_every_actor(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The list filter is the resolver's rule; a matrix pins them together.

    This is the same property the permission model's own suite checks, applied to
    the workflow repository so the two cannot drift when the SQL changes.
    """
    repo = WorkBuddyWorkflowRepo(pool)
    context = context_for(world)
    owner_actor = actor_for(world, world["owner_user_id"], is_tenant_admin=True)

    created: list[str] = []
    for index, scope_kwargs in enumerate(
        (
            {"scope": "enterprise"},
            {"scope": "personal", "owner_user_id": world["owner_user_id"]},
            {"scope": "department", "department_id": world["department_id"]},
        )
    ):
        definition = hello_definition(f"matrix {index}")
        bundle = repo.create_workflow(
            world["tenant_id"],
            name=f"Matrix {index}",
            definition=definition,
            definition_sha256=definition_sha256(definition),
            created_by_user_id=world["owner_user_id"],
            created_by_membership_id=world["owner_member_id"],
        )
        created.append(bundle.workflow.workflow_id)
        RbacService(pool).register_object(
            context,
            object_kind=RBAC_OBJECT_KIND,
            object_id=bundle.workflow.workflow_id,
            actor=owner_actor,
            **scope_kwargs,
        )

    actors = {
        "owner": actor_for(world, world["owner_user_id"]),
        "department_member": actor_for(
            world, world["department_member_user_id"], department_id=world["department_id"]
        ),
        "detached": actor_for(world, world["detached_user_id"]),
        "tenant_admin": actor_for(world, world["owner_user_id"], is_tenant_admin=True),
    }
    for label, rbac_actor in actors.items():
        listed = {
            record.workflow_id
            for record in repo.list_workflows(
                world["tenant_id"],
                user_id=rbac_actor.user_id,
                department_id=rbac_actor.department_id,
                is_tenant_admin=rbac_actor.is_tenant_admin,
            )
        } & set(created)
        resolved = set()
        for workflow_id in created:
            access = RbacService(pool).effective_access(
                context,
                object_kind=RBAC_OBJECT_KIND,
                object_id=workflow_id,
                actor=rbac_actor,
            )
            if access.can_read:
                resolved.add(workflow_id)
        assert listed == resolved, label


async def test_the_scope_row_is_not_rewritten_by_an_update(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any]
) -> None:
    """Editing a workflow's definition must not touch who may see it."""
    owner = principal_for(world, world["owner_user_id"], role="owner")
    created = await create_workflow(app, owner)
    before = scope_rows(pool, world)[created["id"]]

    async with client_for(app, owner) as client:
        detail = await client.get(f"/workflows/{created['id']}")
        etag = detail.headers["etag"]
        updated = await client.put(
            f"/workflows/{created['id']}",
            headers={"if-match": etag},
            json={
                "definition": hello_definition("changed"),
                "base_version_id": created["version"]["id"],
            },
        )
    assert updated.status_code == 201, updated.text
    assert scope_rows(pool, world)[created["id"]] == before

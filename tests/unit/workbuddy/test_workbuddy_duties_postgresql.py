"""Tenant duties on PostgreSQL: a job a member may do without being an admin.

Before this change the five jobs the platform gates — authoring, publishing,
approving, knowledge governance, operations — were reachable only by promoting a
member to tenant admin. The duty grant table lets a tenant hand out the job
instead, and only a database can prove the parts that matter:

* a duty reaches the tenant, a department (and its sub-departments), or exactly
  one member, with the tenant admin holding every duty implicitly;
* a member holding ``kb_admin`` reaches the trigger-governance routes and a
  plain member still gets 403;
* a member holding ``ops`` reaches the capability allowance routes;
* a member holding ``publisher`` may publish a workflow they hold no grant on,
  and one who holds nothing keeps the uniform 404;
* the duty routes are admin-only, revocation takes the job back, and a subject
  outside the tenant is refused.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``).
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
from octop.api.routers import (
    workbuddy_catalog,
    workbuddy_identity,
    workbuddy_knowledge,
    workbuddy_workflows,
)
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal, workbuddy_principal
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import OctopError
from octop.infra.rbac.duties import (
    DUTIES,
    DUTY_AUTHOR,
    DUTY_KB_ADMIN,
    DUTY_OPS,
    DUTY_PUBLISHER,
    WorkBuddyDutyError,
    WorkBuddyDutyRepo,
    actor_holds_duty,
    duties_for_actor,
)
from octop.infra.rbac.model import RbacActor
from octop.infra.users.identity import Role, User

pytestmark = [pytest.mark.postgresql, requires_postgresql]


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
    """One tenant: a two-level department tree and a member outside it."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"duty-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"duty-{uuid.uuid4().hex[:8]}", "Duty tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member_id = str(identity.list_members(tenant_id)[0]["membership_id"])

    parent = identity.create_department(tenant_id, name="Ops", actor_user_id=owner_id)
    parent_id = str(parent["department_id"])
    child = identity.create_department(
        tenant_id, name="Ops L2", parent_id=parent_id, actor_user_id=owner_id
    )
    child_id = str(child["department_id"])

    parent_user = _seed_user(pool, f"duty-parent-{uuid.uuid4().hex[:8]}")
    parent_member = identity.add_membership(
        tenant_id, parent_user, department_id=parent_id, actor_user_id=owner_id
    )
    child_user = _seed_user(pool, f"duty-child-{uuid.uuid4().hex[:8]}")
    child_member = identity.add_membership(
        tenant_id, child_user, department_id=child_id, actor_user_id=owner_id
    )
    outsider = _seed_user(pool, f"duty-outside-{uuid.uuid4().hex[:8]}")
    outsider_member = identity.add_membership(tenant_id, outsider, actor_user_id=owner_id)
    assert parent_member is not None and child_member is not None and outsider_member is not None

    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "owner_member_id": owner_member_id,
        "parent_department_id": parent_id,
        "child_department_id": child_id,
        "parent_user_id": parent_user,
        "child_user_id": child_user,
        "outsider_user_id": outsider,
        "membership_ids": {
            owner_id: owner_member_id,
            parent_user: str(parent_member["membership_id"]),
            child_user: str(child_member["membership_id"]),
            outsider: str(outsider_member["membership_id"]),
        },
    }


@pytest.fixture(scope="module")
def duties(pool: PostgresPool) -> WorkBuddyDutyRepo:
    return WorkBuddyDutyRepo(pool)


def _fresh_tenant(pool: PostgresPool) -> dict[str, Any]:
    """A tenant with a two-level department tree and a member outside it."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"duty2-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"duty2-{uuid.uuid4().hex[:8]}", "Matrix tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member_id = str(identity.list_members(tenant_id)[0]["membership_id"])
    parent = identity.create_department(tenant_id, name="Ops", actor_user_id=owner_id)
    parent_id = str(parent["department_id"])
    child = identity.create_department(
        tenant_id, name="Ops L2", parent_id=parent_id, actor_user_id=owner_id
    )
    child_id = str(child["department_id"])
    parent_user = _seed_user(pool, f"duty2-parent-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, parent_user, department_id=parent_id, actor_user_id=owner_id)
    child_user = _seed_user(pool, f"duty2-child-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, child_user, department_id=child_id, actor_user_id=owner_id)
    outsider = _seed_user(pool, f"duty2-outside-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, outsider, actor_user_id=owner_id)
    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "owner_member_id": owner_member_id,
        "parent_department_id": parent_id,
        "child_department_id": child_id,
        "parent_user_id": parent_user,
        "child_user_id": child_user,
        "outsider_user_id": outsider,
    }


def actor_for(
    world: dict[str, Any], user_id: int, *, department_id: str | None = None
) -> RbacActor:
    return RbacActor(
        user_id=user_id,
        tenant_id=world["tenant_id"],
        department_id=department_id,
        is_tenant_admin=False,
    )


def context_for(world: dict[str, Any], user_id: int) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(world["tenant_id"], user_id=user_id)


def principal_for(
    world: dict[str, Any],
    user_id: int,
    *,
    role: str = "member",
    department_id: str | None = None,
) -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"duty-{user_id}", role=Role.USER, display_name=None),
        tenant_id=world["tenant_id"],
        tenant_slug="duty",
        tenant_name="Duty tenant",
        member_id=world["membership_ids"][user_id],
        role=role,
        department_id=department_id,
        member_status="active",
        tenant_status="active",
    )


@pytest.fixture
def app(pool: PostgresPool) -> FastAPI:
    application = FastAPI()
    for module in (workbuddy_identity, workbuddy_knowledge, workbuddy_catalog, workbuddy_workflows):
        application.include_router(module.router)

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


def hello_definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {"input": {"greeting": "hello {{ inputs.who }}"}, "expression": "inputs"},
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }


async def create_published_workflow(app: FastAPI, owner: WorkBuddyPrincipal) -> dict[str, Any]:
    """A workflow the owner created and published; a stranger holds no grant on it."""
    async with client_for(app, owner) as client:
        created = await client.post(
            "/workflows",
            json={"name": f"Duty flow {uuid.uuid4().hex[:6]}", "definition": hello_definition()},
        )
        assert created.status_code == 201, created.text
        body = created.json()["data"]
        detail = await client.get(f"/workflows/{body['id']}")
        etag = detail.headers["etag"]
        activated = await client.post(
            f"/workflows/{body['id']}/activate",
            headers={"if-match": etag},
            json={"version_id": body["version"]["id"]},
        )
        assert activated.status_code == 200, activated.text
        return body


# ── who holds a duty ────────────────────────────────────────────────────────


def test_a_duty_reaches_the_tenant_a_department_and_a_member(
    pool: PostgresPool, duties: WorkBuddyDutyRepo
) -> None:
    """The same three subject kinds as every other grant in the platform.

    This matrix runs on its own tenant: a tenant-wide duty is exactly the kind of
    row that must not leak into the gates the other tests check.
    """
    world = _fresh_tenant(pool)
    tenant_wide = DUTY_OPS
    department_wide = DUTY_KB_ADMIN
    personal = DUTY_AUTHOR
    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=tenant_wide,
        subject_kind="tenant",
        actor_member_id=world["owner_member_id"],
    )
    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=department_wide,
        subject_kind="department",
        subject_id=world["parent_department_id"],
        actor_member_id=world["owner_member_id"],
    )
    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=personal,
        subject_kind="member",
        subject_id=str(world["outsider_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    outsider = actor_for(world, world["outsider_user_id"])
    assert duties_for_actor(pool, outsider) == frozenset({tenant_wide, personal})

    # A department grant reaches the department and its sub-departments.
    parent = actor_for(world, world["parent_user_id"], department_id=world["parent_department_id"])
    child = actor_for(world, world["child_user_id"], department_id=world["child_department_id"])
    assert department_wide in duties_for_actor(pool, parent)
    assert department_wide in duties_for_actor(pool, child)
    assert actor_holds_duty(pool, child, personal) is False


def test_the_tenant_admin_holds_every_duty(pool: PostgresPool, world: dict[str, Any]) -> None:
    """The gate must never take a power away from the people who run the tenant."""
    admin = RbacActor(
        user_id=world["owner_user_id"],
        tenant_id=world["tenant_id"],
        department_id=None,
        is_tenant_admin=True,
    )
    assert duties_for_actor(pool, admin) == frozenset(DUTIES)
    for duty in DUTIES:
        assert actor_holds_duty(pool, admin, duty) is True


def test_revoking_a_duty_takes_the_job_back(
    pool: PostgresPool, world: dict[str, Any], duties: WorkBuddyDutyRepo
) -> None:
    """Granting twice refreshes one row; revoking removes it."""
    ctx = context_for(world, world["owner_user_id"])
    duties.grant(
        ctx,
        duty=DUTY_AUTHOR,
        subject_kind="member",
        subject_id=str(world["parent_user_id"]),
        actor_member_id=world["owner_member_id"],
    )
    duties.grant(
        ctx,
        duty=DUTY_AUTHOR,
        subject_kind="member",
        subject_id=str(world["parent_user_id"]),
        actor_member_id=world["owner_member_id"],
    )
    mine = [
        g for g in duties.list_grants(ctx, duty=DUTY_AUTHOR) if g.user_id == world["parent_user_id"]
    ]
    assert len(mine) == 1

    assert (
        duties.revoke(
            ctx, duty=DUTY_AUTHOR, subject_kind="member", subject_id=str(world["parent_user_id"])
        )
        is True
    )
    assert actor_holds_duty(pool, actor_for(world, world["parent_user_id"]), DUTY_AUTHOR) is False
    assert (
        duties.revoke(
            ctx, duty=DUTY_AUTHOR, subject_kind="member", subject_id=str(world["parent_user_id"])
        )
        is False
    )


def test_a_subject_outside_the_tenant_is_refused(
    pool: PostgresPool, world: dict[str, Any], duties: WorkBuddyDutyRepo
) -> None:
    """Cross-tenant departments and non-members never become duty holders."""
    identity = WorkBuddyIdentityRepo(pool)
    other_owner = _seed_user(pool, f"duty-other-{uuid.uuid4().hex[:8]}")
    other = identity.create_tenant(
        f"duty-other-{uuid.uuid4().hex[:8]}", "Other tenant", owner_user_id=other_owner
    )
    other_department = identity.create_department(
        str(other["tenant_id"]), name="Theirs", actor_user_id=other_owner
    )
    ctx = context_for(world, world["owner_user_id"])

    with pytest.raises(WorkBuddyDutyError):
        duties.grant(
            ctx,
            duty=DUTY_OPS,
            subject_kind="department",
            subject_id=str(other_department["department_id"]),
            actor_member_id=world["owner_member_id"],
        )

    stranger = _seed_user(pool, f"duty-stranger-{uuid.uuid4().hex[:8]}")
    with pytest.raises(WorkBuddyDutyError):
        duties.grant(
            ctx,
            duty=DUTY_OPS,
            subject_kind="member",
            subject_id=str(stranger),
            actor_member_id=world["owner_member_id"],
        )

    with pytest.raises(WorkBuddyDutyError):
        duties.grant(
            ctx,
            duty="not-a-duty",
            subject_kind="tenant",
            actor_member_id=world["owner_member_id"],
        )

    with pool.connect() as conn, pytest.raises(Exception) as malformed:
        conn.execute(
            "INSERT INTO workbuddy_tenant_duty_grants("
            " tenant_id, duty, subject_key, user_id, granted_at"
            ") VALUES (?, 'ops', 'tenant', ?, ?)",
            (world["tenant_id"], world["outsider_user_id"], now_ts()),
        )
    assert "check" in str(malformed.value).lower()


# ── the jobs the duties unlock ──────────────────────────────────────────────


async def test_a_kb_admin_reaches_the_knowledge_governance_routes(
    app: FastAPI, world: dict[str, Any], duties: WorkBuddyDutyRepo
) -> None:
    """The route that used to require tenant admin now accepts the duty."""
    plain = principal_for(
        world, world["parent_user_id"], department_id=world["parent_department_id"]
    )
    unknown_workflow = str(uuid.uuid4())
    path = f"/workflows/{unknown_workflow}/trigger-registrations"
    async with client_for(app, plain) as client:
        refused = await client.get(path)
        assert refused.status_code == 403, refused.text

    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=DUTY_KB_ADMIN,
        subject_kind="member",
        subject_id=str(world["parent_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    async with client_for(app, plain) as client:
        allowed = await client.get(path)
        # The gate passed: an unknown workflow is what fails next, not the caller.
        assert allowed.status_code == 404, allowed.text


async def test_an_ops_holder_reaches_the_capability_routes(
    app: FastAPI, world: dict[str, Any], duties: WorkBuddyDutyRepo
) -> None:
    """Platform allowances move from admin-only to admin-or-ops."""
    plain = principal_for(world, world["outsider_user_id"])
    async with client_for(app, plain) as client:
        refused = await client.get("/tenant-capabilities")
        assert refused.status_code == 403, refused.text

    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=DUTY_OPS,
        subject_kind="member",
        subject_id=str(world["outsider_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    async with client_for(app, plain) as client:
        allowed = await client.get("/tenant-capabilities")
        assert allowed.status_code == 200, allowed.text


async def test_a_publisher_may_publish_a_workflow_without_a_grant(
    app: FastAPI, world: dict[str, Any], duties: WorkBuddyDutyRepo
) -> None:
    """The money shot of B-05: the publishing job without a per-object grant."""
    owner = principal_for(world, world["owner_user_id"], role="owner")
    workflow = await create_published_workflow(app, owner)
    publisher = principal_for(world, world["outsider_user_id"])

    async with client_for(app, publisher) as client:
        hidden = await client.get(f"/workflows/{workflow['id']}/versions")
        assert hidden.status_code == 404, hidden.text

    duties.grant(
        context_for(world, world["owner_user_id"]),
        duty=DUTY_PUBLISHER,
        subject_kind="member",
        subject_id=str(world["outsider_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    async with client_for(app, publisher) as client:
        detail = await client.get(f"/workflows/{workflow['id']}")
        assert detail.status_code == 200, detail.text
        etag = detail.headers["etag"]
        versions = await client.get(f"/workflows/{workflow['id']}/versions")
        assert versions.status_code == 200, versions.text
        rolled = await client.post(
            f"/workflows/{workflow['id']}/rollback",
            headers={"if-match": etag},
            json={"version_id": workflow["version"]["id"]},
        )
        assert rolled.status_code == 201, rolled.text


# ── the administration surface of the duties ─────────────────────────────────


async def test_the_duty_routes_are_admin_only_and_round_trip(
    app: FastAPI, world: dict[str, Any]
) -> None:
    """A member cannot hand out jobs; an admin can, and can take them back."""
    member = principal_for(
        world, world["parent_user_id"], department_id=world["parent_department_id"]
    )
    admin = principal_for(world, world["owner_user_id"], role="owner")
    body = {"subject_kind": "member", "subject_id": str(world["parent_user_id"])}

    async with client_for(app, member) as client:
        refused = await client.post(f"/duties/{DUTY_AUTHOR}/grants", json=body)
        assert refused.status_code == 403, refused.text
        hidden = await client.get("/duties")
        assert hidden.status_code == 403, hidden.text

    async with client_for(app, admin) as client:
        created = await client.post(f"/duties/{DUTY_AUTHOR}/grants", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["data"]["duty"] == DUTY_AUTHOR

        listed = await client.get("/duties")
        assert listed.status_code == 200, listed.text
        payload = listed.json()["data"]
        assert payload["duties"] == list(DUTIES)
        assert any(
            item["duty"] == DUTY_AUTHOR and item["subject_id"] == str(world["parent_user_id"])
            for item in payload["items"]
        )

        revoked = await client.delete(
            f"/duties/{DUTY_AUTHOR}/grants",
            params={"subject_kind": "member", "subject_id": str(world["parent_user_id"])},
        )
        assert revoked.status_code == 200, revoked.text
        missing = await client.delete(
            f"/duties/{DUTY_AUTHOR}/grants",
            params={"subject_kind": "member", "subject_id": str(world["parent_user_id"])},
        )
        assert missing.status_code == 404, missing.text

        bad = await client.post("/duties/not-a-duty/grants", json=body)
        assert bad.status_code == 400, bad.text


def test_the_duty_table_is_isolated_like_every_other_tenant_table(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """RLS is enabled and forced, so a duty never leaks across tenants."""
    with pool.connect() as conn:
        row = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relname = 'workbuddy_tenant_duty_grants'"
        ).fetchone()
    assert row is not None and row["relrowsecurity"] and row["relforcerowsecurity"]

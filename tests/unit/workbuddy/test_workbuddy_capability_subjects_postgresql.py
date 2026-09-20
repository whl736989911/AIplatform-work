"""Tool and model grants on PostgreSQL: who a revision reaches, and who it does not.

An approved tool or model used to be approved for the whole tenant: one grant
row per revision and no subject. A tenant could not approve a model for one
department, and the workflow compiler answered reachability for the tenant
instead of for the caller. Only a database can prove the parts that matter:

* a department grant reaches that department and its sub-departments, a member
  grant reaches exactly that member, and the tenant-wide row reaches everyone;
* the compiler's resolver refuses a tool or a model the caller cannot reach, and
  a default model the caller cannot reach counts as unconfigured;
* replacing the tenant-wide approved set leaves department and member grants
  alone, and a revoked revision takes every subject with it;
* the row shape is enforced by the schema, so a subject outside the tenant is
  unrepresentable.

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
from octop.api.routers import workbuddy_catalog
from octop.api.routers.workbuddy_catalog import CAPABILITY_MODEL, CAPABILITY_TOOL
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal, workbuddy_principal
from octop.infra.db.migrate import _ensure_workbuddy_grant_subjects, run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_catalog import (
    SUBJECT_DEPARTMENT,
    SUBJECT_MEMBER,
    SUBJECT_TENANT,
    WorkBuddyCatalogError,
    WorkBuddyCatalogRepo,
    WorkBuddyInvalidInput,
)
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_workflows import PostgresWorkflowSemanticResolver
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import OctopError
from octop.infra.users.identity import Role, User

pytestmark = [pytest.mark.postgresql, requires_postgresql]

ADAPTER_KEY = "workbuddy-test"


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
    owner_id = _seed_user(pool, f"cap-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"cap-{uuid.uuid4().hex[:8]}", "Capability tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member_id = str(identity.list_members(tenant_id)[0]["membership_id"])

    parent = identity.create_department(tenant_id, name="Ops", actor_user_id=owner_id)
    parent_id = str(parent["department_id"])
    child = identity.create_department(
        tenant_id, name="Ops L2", parent_id=parent_id, actor_user_id=owner_id
    )
    child_id = str(child["department_id"])

    parent_user = _seed_user(pool, f"cap-parent-{uuid.uuid4().hex[:8]}")
    parent_member = identity.add_membership(
        tenant_id, parent_user, department_id=parent_id, actor_user_id=owner_id
    )
    child_user = _seed_user(pool, f"cap-child-{uuid.uuid4().hex[:8]}")
    child_member = identity.add_membership(
        tenant_id, child_user, department_id=child_id, actor_user_id=owner_id
    )
    outsider = _seed_user(pool, f"cap-outside-{uuid.uuid4().hex[:8]}")
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
def catalog(pool: PostgresPool) -> WorkBuddyCatalogRepo:
    return WorkBuddyCatalogRepo(pool)


def principal_for(
    world: dict[str, Any],
    user_id: int,
    *,
    role: str = "member",
    department_id: str | None = None,
) -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"cap-{user_id}", role=Role.USER, display_name=None),
        tenant_id=world["tenant_id"],
        tenant_slug="cap",
        tenant_name="Capability tenant",
        member_id=world["membership_ids"][user_id],
        role=role,
        department_id=department_id,
        member_status="active",
        tenant_status="active",
    )


@pytest.fixture
def app(pool: PostgresPool) -> FastAPI:
    application = FastAPI()
    application.include_router(workbuddy_catalog.router)

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


def _tool_key() -> str:
    return f"tool_{uuid.uuid4().hex[:8]}"


def _model_key() -> str:
    return f"model_{uuid.uuid4().hex[:8]}"


def publish_tool(catalog: WorkBuddyCatalogRepo, world: dict[str, Any]) -> tuple[str, str]:
    """``(revision id, tool key)`` for a fresh published platform tool."""
    key = _tool_key()
    revision = catalog.publish_tool(
        adapter_key=ADAPTER_KEY,
        tool_key=key,
        display_name=f"Tool {key}",
        actor_user_id=world["owner_user_id"],
    )
    return revision.tool_revision_id, key


def publish_model(catalog: WorkBuddyCatalogRepo, world: dict[str, Any]) -> tuple[str, str]:
    """``(revision id, model key)`` for a fresh published platform model."""
    key = _model_key()
    revision = catalog.publish_model(
        adapter_key=ADAPTER_KEY,
        model_key=key,
        display_name=f"Model {key}",
        actor_user_id=world["owner_user_id"],
    )
    return revision.model_revision_id, key


def decide_tool(pool: PostgresPool, world: dict[str, Any], user_id: int, tool_key: str) -> Any:
    """The compiler's verdict for one caller, on that caller's transaction."""
    with workbuddy_transaction(
        pool, WorkBuddyDbContext.for_tenant(world["tenant_id"], user_id=user_id)
    ) as conn:
        resolver = PostgresWorkflowSemanticResolver(conn, world["tenant_id"], user_id=user_id)
        return resolver.check_tool(tool_key, {})


def decide_model(pool: PostgresPool, world: dict[str, Any], user_id: int, model: str | None) -> Any:
    with workbuddy_transaction(
        pool, WorkBuddyDbContext.for_tenant(world["tenant_id"], user_id=user_id)
    ) as conn:
        resolver = PostgresWorkflowSemanticResolver(conn, world["tenant_id"], user_id=user_id)
        return resolver.check_model(model)


def grant_rows(pool: PostgresPool, world: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    table = (
        "workbuddy_tenant_tool_grants"
        if kind == CAPABILITY_TOOL
        else "workbuddy_tenant_model_grants"
    )
    column = "tool_revision_id" if kind == CAPABILITY_TOOL else "model_revision_id"
    with pool.connect() as conn:
        rows = conn.execute(
            f"SELECT {column} AS revision_id, subject_key, user_id, department_id"
            f" FROM {table} WHERE tenant_id = ? ORDER BY subject_key",
            (world["tenant_id"],),
        ).fetchall()
    return [
        {
            "revision_id": str(row["revision_id"]),
            "subject_key": str(row["subject_key"]),
            "user_id": None if row["user_id"] is None else int(row["user_id"]),
            "department_id": (None if row["department_id"] is None else str(row["department_id"])),
        }
        for row in rows
    ]


def test_the_tenant_wide_grant_reaches_every_member(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """The row every tenant had before the subject dimension still reaches everyone."""
    revision_id, key = publish_tool(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_TENANT,
        actor_member_id=world["owner_member_id"],
    )

    for user_id in (
        world["owner_user_id"],
        world["parent_user_id"],
        world["child_user_id"],
        world["outsider_user_id"],
    ):
        decision = decide_tool(pool, world, user_id, key)
        assert decision.ok is True, (user_id, decision)


def test_a_department_grant_reaches_its_sub_departments(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """A grant on a department covers the members of that department's subtree."""
    revision_id, key = publish_tool(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_DEPARTMENT,
        subject_id=world["parent_department_id"],
        actor_member_id=world["owner_member_id"],
    )

    assert decide_tool(pool, world, world["parent_user_id"], key).ok is True
    assert decide_tool(pool, world, world["child_user_id"], key).ok is True
    refused = decide_tool(pool, world, world["outsider_user_id"], key)
    assert refused.ok is False
    assert refused.code == "WORKFLOW_TOOL_UNAVAILABLE"


def test_a_child_department_grant_does_not_reach_the_parent(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """Reach flows down the tree, never up: a sub-department grant stays there."""
    revision_id, key = publish_tool(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_DEPARTMENT,
        subject_id=world["child_department_id"],
        actor_member_id=world["owner_member_id"],
    )

    assert decide_tool(pool, world, world["child_user_id"], key).ok is True
    assert decide_tool(pool, world, world["parent_user_id"], key).ok is False
    assert decide_tool(pool, world, world["outsider_user_id"], key).ok is False


def test_a_member_grant_reaches_exactly_that_member(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """The narrowest subject: one user id, nobody else."""
    revision_id, key = publish_tool(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_MEMBER,
        subject_id=str(world["outsider_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    assert decide_tool(pool, world, world["outsider_user_id"], key).ok is True
    assert decide_tool(pool, world, world["parent_user_id"], key).ok is False
    assert decide_tool(pool, world, world["child_user_id"], key).ok is False


def test_a_narrowed_model_reaches_only_its_subject(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """A model approved for one department is refused for a member outside it."""
    revision_id, key = publish_model(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_MODEL,
        revision_id=revision_id,
        subject_kind=SUBJECT_DEPARTMENT,
        subject_id=world["parent_department_id"],
        actor_member_id=world["owner_member_id"],
    )

    assert decide_model(pool, world, world["parent_user_id"], key).ok is True
    assert decide_model(pool, world, world["child_user_id"], key).ok is True
    refused = decide_model(pool, world, world["outsider_user_id"], key)
    assert refused.ok is False
    assert refused.code == "MODEL_NOT_CONFIGURED"


def test_the_default_model_needs_a_tenant_wide_grant(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """The stored default is unusable once its tenant-wide grant is dropped."""
    revision_id, _ = publish_model(catalog, world)
    catalog.update_capabilities(
        world["tenant_id"],
        actor_member_id=world["owner_member_id"],
        model_revision_ids=(revision_id,),
        default_model_revision_id=revision_id,
    )
    assert decide_model(pool, world, world["outsider_user_id"], None).ok is True

    assert (
        catalog.revoke_capability_grant(
            world["tenant_id"],
            kind=CAPABILITY_MODEL,
            revision_id=revision_id,
            subject_kind=SUBJECT_TENANT,
        )
        is True
    )

    capabilities = catalog.get_capabilities(world["tenant_id"])
    assert capabilities is not None
    assert capabilities.default_model_revision_id is None
    assert decide_model(pool, world, world["outsider_user_id"], None).ok is False


def test_replacing_the_tenant_wide_set_keeps_subject_grants(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """The capability upsert owns the tenant-wide rows and nothing else."""
    keep_id, _ = publish_tool(catalog, world)
    drop_id, _ = publish_tool(catalog, world)
    scoped_id, _ = publish_tool(catalog, world)
    for revision_id in (keep_id, drop_id):
        catalog.grant_capability(
            world["tenant_id"],
            kind=CAPABILITY_TOOL,
            revision_id=revision_id,
            subject_kind=SUBJECT_TENANT,
            actor_member_id=world["owner_member_id"],
        )
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=scoped_id,
        subject_kind=SUBJECT_MEMBER,
        subject_id=str(world["outsider_user_id"]),
        actor_member_id=world["owner_member_id"],
    )

    catalog.update_capabilities(
        world["tenant_id"],
        actor_member_id=world["owner_member_id"],
        tool_revision_ids=(keep_id,),
    )

    rows = {
        (row["revision_id"], row["subject_key"])
        for row in grant_rows(pool, world, kind=CAPABILITY_TOOL)
    }
    assert (keep_id, SUBJECT_TENANT) in rows
    assert (drop_id, SUBJECT_TENANT) not in rows
    assert (scoped_id, f"{SUBJECT_MEMBER}:{world['outsider_user_id']}") in rows


def test_a_revoked_revision_takes_every_subject_with_it(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """Revocation is terminal for the revision and for each of its grants."""
    revision_id, key = publish_tool(catalog, world)
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_TENANT,
        actor_member_id=world["owner_member_id"],
    )
    catalog.grant_capability(
        world["tenant_id"],
        kind=CAPABILITY_TOOL,
        revision_id=revision_id,
        subject_kind=SUBJECT_DEPARTMENT,
        subject_id=world["parent_department_id"],
        actor_member_id=world["owner_member_id"],
    )

    assert catalog.revoke_tool(revision_id, actor_user_id=world["owner_user_id"]) is True

    remaining = {row["revision_id"] for row in grant_rows(pool, world, kind=CAPABILITY_TOOL)}
    assert revision_id not in remaining
    assert decide_tool(pool, world, world["parent_user_id"], key).ok is False


def test_the_schema_rejects_a_subject_outside_the_tenant(
    pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """Cross-tenant and malformed subjects are refused before they reach a query."""
    other_owner = _seed_user(pool, f"cap-other-{uuid.uuid4().hex[:8]}")
    identity = WorkBuddyIdentityRepo(pool)
    other = identity.create_tenant(
        f"cap-other-{uuid.uuid4().hex[:8]}", "Other tenant", owner_user_id=other_owner
    )
    other_department = identity.create_department(
        str(other["tenant_id"]), name="Theirs", actor_user_id=other_owner
    )
    revision_id, _ = publish_tool(catalog, world)

    with pytest.raises(WorkBuddyInvalidInput):
        catalog.grant_capability(
            world["tenant_id"],
            kind=CAPABILITY_TOOL,
            revision_id=revision_id,
            subject_kind=SUBJECT_DEPARTMENT,
            subject_id=str(other_department["department_id"]),
            actor_member_id=world["owner_member_id"],
        )

    outsider = _seed_user(pool, f"cap-stranger-{uuid.uuid4().hex[:8]}")
    with pytest.raises(WorkBuddyCatalogError):
        catalog.grant_capability(
            world["tenant_id"],
            kind=CAPABILITY_TOOL,
            revision_id=revision_id,
            subject_kind=SUBJECT_MEMBER,
            subject_id=str(outsider),
            actor_member_id=world["owner_member_id"],
        )

    with pool.connect() as conn, pytest.raises(Exception) as malformed:
        conn.execute(
            "INSERT INTO workbuddy_tenant_tool_grants("
            " tenant_id, tool_revision_id, subject_key, user_id, granted_at"
            ") VALUES (?, ?, 'tenant', ?, ?)",
            (world["tenant_id"], revision_id, world["outsider_user_id"], now_ts()),
        )
    assert "check" in str(malformed.value).lower()


async def test_granting_through_the_admin_api_narrows_and_restores_reach(
    app: FastAPI, pool: PostgresPool, world: dict[str, Any], catalog: WorkBuddyCatalogRepo
) -> None:
    """The route an administrator uses moves real reach, and only as an admin."""
    revision_id, key = publish_tool(catalog, world)
    admin = principal_for(world, world["owner_user_id"], role="owner")
    member = principal_for(world, world["outsider_user_id"])

    async with client_for(app, member) as client:
        forbidden = await client.post(
            f"/tenant-capabilities/{CAPABILITY_TOOL}/{revision_id}/grants",
            json={"subject_kind": SUBJECT_TENANT},
        )
    assert forbidden.status_code == 403, forbidden.text

    async with client_for(app, admin) as client:
        granted = await client.post(
            f"/tenant-capabilities/{CAPABILITY_TOOL}/{revision_id}/grants",
            json={"subject_kind": SUBJECT_TENANT},
        )
        assert granted.status_code == 201, granted.text
        assert granted.json()["data"]["subject_kind"] == SUBJECT_TENANT

        listed = await client.get("/tenant-capabilities")
        assert listed.status_code == 200, listed.text
        subjects = listed.json()["data"]["tool_grants"]
        assert {"subject_kind": SUBJECT_TENANT, "revision_id": revision_id} in [
            {"subject_kind": item["subject_kind"], "revision_id": item["revision_id"]}
            for item in subjects
        ]

    assert decide_tool(pool, world, world["outsider_user_id"], key).ok is True

    async with client_for(app, admin) as client:
        revoked = await client.delete(
            f"/tenant-capabilities/{CAPABILITY_TOOL}/{revision_id}/grants",
            params={"subject_kind": SUBJECT_TENANT},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["revoked"] is True

    assert decide_tool(pool, world, world["outsider_user_id"], key).ok is False


def test_the_subject_columns_appear_on_a_database_that_skipped_the_fold(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """A database born before the fold is converted once, and the conversion repeats safely."""
    old_shape = (
        ("workbuddy_tenant_tool_grants", "tool_revision_id"),
        ("workbuddy_tenant_model_grants", "model_revision_id"),
    )
    with pool.connect() as conn:
        for table, column in old_shape:
            conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey")
            conn.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS"
                f" {'workbuddy_tool_grants_subject_shape' if 'tool' in table else 'workbuddy_model_grants_subject_shape'}"
            )
            conn.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS"
                f" {'workbuddy_tool_grants_department_fkey' if 'tool' in table else 'workbuddy_model_grants_department_fkey'}"
            )
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS subject_key")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS user_id")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS department_id")
            conn.execute(f"ALTER TABLE {table} ADD PRIMARY KEY (tenant_id, {column})")

    _ensure_workbuddy_grant_subjects(pool)
    _ensure_workbuddy_grant_subjects(pool)  # idempotent: the guard is the folded column

    with pool.connect() as conn:
        for table, _column in old_shape:
            columns = {
                str(row["column_name"])
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                    (table,),
                ).fetchall()
            }
            assert {"subject_key", "user_id", "department_id"} <= columns
            key_columns = [
                str(row["column_name"])
                for row in conn.execute(
                    "SELECT a.attname AS column_name FROM pg_index i"
                    " JOIN pg_attribute a ON a.attrelid = i.indrelid"
                    " AND a.attnum = ANY(i.indkey)"
                    " WHERE i.indrelid = ?::regclass AND i.indisprimary"
                    " ORDER BY array_position(i.indkey, a.attnum)",
                    (table,),
                ).fetchall()
            ]
            assert key_columns == ["tenant_id", _column, "subject_key"]

    # The repaired database keeps enforcing the subject shape.
    revision_id, _ = publish_tool(WorkBuddyCatalogRepo(pool), world)
    with pool.connect() as conn, pytest.raises(Exception) as malformed:
        conn.execute(
            "INSERT INTO workbuddy_tenant_tool_grants("
            " tenant_id, tool_revision_id, subject_key, user_id, granted_at"
            ") VALUES (?, ?, 'tenant', ?, ?)",
            (world["tenant_id"], revision_id, world["outsider_user_id"], now_ts()),
        )
    assert "check" in str(malformed.value).lower()

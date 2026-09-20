"""The generic permission tables on PostgreSQL: constraints, isolation, four layers.

The model suite proves the rank logic; what only a database can prove is here:

* both permission tables really are ``FORCE ROW LEVEL SECURITY`` with a tenant
  policy, so a tenant context cannot read another tenant's access rows even with
  the right ids;
* the CHECK constraints the model mirrors (scope shape, permission vocabulary,
  exactly one subject) are enforced by the database too, so a row that the model
  would refuse cannot be written around it;
* a grant cannot exist without its object being registered, and an object's
  identity columns cannot be re-pointed at another object;
* the four layers resolve end to end through :class:`RbacService`, and the SQL
  visibility filter agrees with the pure resolver for every actor.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``); the
database is dedicated to the suite and is reset here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.model import RbacActor, ScopeGrant
from octop.infra.rbac.resolver import resolve_access
from octop.infra.rbac.service import RbacService

pytestmark = [pytest.mark.postgresql, requires_postgresql]

TABLES = ("workbuddy_object_scopes", "workbuddy_object_acl")
KIND = "workflow"

_PROBE_ROLE = "octop_rbac_probe"
_PROBE_PASSWORD = "octop_rbac_probe_pw"


def _conninfo() -> str:
    return os.environ["OCTOP_TEST_DATABASE_URL"]


def _conninfo_for(role: str, password: str) -> str:
    parts = urlsplit(_conninfo())
    netloc = f"{role}:{password}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@pytest.fixture(scope="module")
def pool() -> Iterator[PostgresPool]:
    db = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"], min_size=1, max_size=2)
    with db.transaction() as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        available = conn.execute(
            "SELECT 1 AS present FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if available is None:
            pytest.skip("pgvector is required by the migrations under test")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    run_migrations(db)
    yield db
    db.close()


def _seed_user(pool: PostgresPool, username: str) -> int:
    with pool.connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


@pytest.fixture(scope="module")
def probe(pool: PostgresPool) -> Iterator[PostgresPool]:
    """A least-privilege login: not the table owner, no BYPASSRLS.

    The suite's own connection is a superuser, and a superuser bypasses row-level
    security even when it is forced — so the policies can only be proven through
    this connection.
    """
    with pool.connect() as conn:
        conn.execute(f"DROP ROLE IF EXISTS {_PROBE_ROLE}")
        conn.execute(
            f"CREATE ROLE {_PROBE_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_PROBE_PASSWORD}'"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {_PROBE_ROLE}")
        for table in TABLES:
            conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_PROBE_ROLE}")
        conn.execute(f"GRANT EXECUTE ON FUNCTION workbuddy_rls_visible(uuid) TO {_PROBE_ROLE}")
        conn.execute(f"GRANT EXECUTE ON FUNCTION workbuddy_current_tenant_id() TO {_PROBE_ROLE}")
    probe_pool = PostgresPool(_conninfo_for(_PROBE_ROLE, _PROBE_PASSWORD), min_size=1, max_size=2)
    try:
        yield probe_pool
    finally:
        probe_pool.close()


def _raw(pool: PostgresPool, ctx: WorkBuddyDbContext, sql: str, params: tuple[Any, ...]) -> None:
    """Run one raw statement in its own tenant transaction (it must not commit)."""
    with workbuddy_transaction(pool, ctx) as conn:
        conn.execute(sql, params)


@pytest.fixture(scope="module")
def world(pool: PostgresPool) -> dict[str, Any]:
    """Two tenants; in the first one an owner, a department member and a detached member."""
    repo = WorkBuddyIdentityRepo(pool)
    owner_user_id = _seed_user(pool, f"rbac-owner-{uuid.uuid4().hex[:8]}")
    tenant = repo.create_tenant(
        f"rbac-{uuid.uuid4().hex[:8]}", "RBAC tenant", owner_user_id=owner_user_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member = repo.list_members(tenant_id)[0]
    department = repo.create_department(tenant_id, name="Engineering", actor_user_id=owner_user_id)
    assert department is not None
    department_id = str(department["department_id"])

    member_user_id = _seed_user(pool, f"rbac-member-{uuid.uuid4().hex[:8]}")
    member = repo.add_membership(
        tenant_id,
        member_user_id,
        department_id=department_id,
        actor_user_id=owner_user_id,
    )
    detached_user_id = _seed_user(pool, f"rbac-detached-{uuid.uuid4().hex[:8]}")
    detached = repo.add_membership(tenant_id, detached_user_id, actor_user_id=owner_user_id)
    assert member is not None and detached is not None

    other_owner_id = _seed_user(pool, f"rbac-other-{uuid.uuid4().hex[:8]}")
    other = repo.create_tenant(
        f"rbac-other-{uuid.uuid4().hex[:8]}", "RBAC other tenant", owner_user_id=other_owner_id
    )

    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_user_id,
        "owner_member_id": str(owner_member["membership_id"]),
        "department_id": department_id,
        "member_user_id": member_user_id,
        "member_id": str(member["membership_id"]),
        "detached_user_id": detached_user_id,
        "detached_member_id": str(detached["membership_id"]),
        "other_tenant_id": str(other["tenant_id"]),
        "other_owner_user_id": other_owner_id,
    }


def ctx_for(world: dict[str, Any], user_id: int | None = None) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(
        world["tenant_id"], user_id=user_id if user_id is not None else world["owner_user_id"]
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


def new_object_id() -> str:
    return str(uuid.uuid4())


def test_permission_tables_force_row_level_security(pool: PostgresPool) -> None:
    state: dict[str, tuple[bool, bool, int]] = {}
    with pool.connect() as conn:
        for table in TABLES:
            flags = conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid = to_regclass(?)",
                (table,),
            ).fetchone()
            policies = conn.execute(
                "SELECT count(*) AS policies FROM pg_policies WHERE tablename = ?", (table,)
            ).fetchone()
            state[table] = (
                bool(flags["relrowsecurity"]),
                bool(flags["relforcerowsecurity"]),
                int(policies["policies"]),
            )
    assert state == {
        "workbuddy_object_scopes": (True, True, 1),
        "workbuddy_object_acl": (True, True, 1),
    }, state


def test_the_scope_shape_and_permission_vocabulary_are_enforced(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The model refuses these too; the database is the last line of defence."""
    service = RbacService(pool)
    ctx = ctx_for(world)
    registered = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=registered,
        scope="personal",
        owner_user_id=world["owner_user_id"],
        actor=actor_for(world, world["owner_user_id"]),
    )

    scope_insert = (
        "INSERT INTO workbuddy_object_scopes (tenant_id, object_kind, object_id, scope, "
        "owner_user_id, department_id, created_by_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        # personal without an owner
        _raw(
            pool,
            ctx,
            scope_insert,
            (
                world["tenant_id"],
                KIND,
                new_object_id(),
                "personal",
                None,
                None,
                world["owner_user_id"],
                now_ts(),
                now_ts(),
            ),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        # enterprise that still names an owner
        _raw(
            pool,
            ctx,
            scope_insert,
            (
                world["tenant_id"],
                KIND,
                new_object_id(),
                "enterprise",
                world["owner_user_id"],
                None,
                world["owner_user_id"],
                now_ts(),
                now_ts(),
            ),
        )

    acl_insert = (
        "INSERT INTO workbuddy_object_acl (tenant_id, object_kind, object_id, user_id, "
        "department_id, permission, granted_by_user_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        # a permission the vocabulary does not contain
        _raw(
            pool,
            ctx,
            acl_insert,
            (
                world["tenant_id"],
                KIND,
                registered,
                world["member_user_id"],
                None,
                "owner",
                world["owner_user_id"],
                now_ts(),
                now_ts(),
            ),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        # both subjects at once
        _raw(
            pool,
            ctx,
            acl_insert,
            (
                world["tenant_id"],
                KIND,
                registered,
                world["member_user_id"],
                world["department_id"],
                "read",
                world["owner_user_id"],
                now_ts(),
                now_ts(),
            ),
        )


def test_a_grant_needs_its_object_to_be_registered(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    unregistered = new_object_id()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _raw(
            pool,
            ctx_for(world),
            "INSERT INTO workbuddy_object_acl (tenant_id, object_kind, object_id, user_id, "
            "permission, granted_by_user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'read', ?, ?, ?)",
            (
                world["tenant_id"],
                KIND,
                unregistered,
                world["member_user_id"],
                world["owner_user_id"],
                now_ts(),
                now_ts(),
            ),
        )


def test_an_objects_identity_cannot_be_re_pointed(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    service = RbacService(pool)
    ctx = ctx_for(world)
    object_id = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=object_id,
        scope="enterprise",
        actor=actor_for(world, world["owner_user_id"]),
    )

    with pytest.raises(psycopg.errors.CheckViolation):
        _raw(
            pool,
            ctx,
            "UPDATE workbuddy_object_scopes SET object_id = ? "
            "WHERE tenant_id = ? AND object_kind = ? AND object_id = ?",
            (new_object_id(), world["tenant_id"], KIND, object_id),
        )


def test_grants_are_invisible_across_tenants(
    pool: PostgresPool, probe: PostgresPool, world: dict[str, Any]
) -> None:
    service = RbacService(pool)
    ctx = ctx_for(world)
    object_id = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=object_id,
        scope="enterprise",
        actor=actor_for(world, world["owner_user_id"]),
    )
    service.grant(
        ctx,
        object_kind=KIND,
        object_id=object_id,
        actor=actor_for(world, world["owner_user_id"], is_tenant_admin=True),
        permission="admin",
        user_id=world["member_user_id"],
    )

    other_ctx = WorkBuddyDbContext.for_tenant(
        world["other_tenant_id"], user_id=world["other_owner_user_id"]
    )
    owner_ctx = ctx_for(world)

    # The tenant's own context sees its rows; the other tenant sees none of them.
    with workbuddy_transaction(probe, owner_ctx) as conn:
        own = conn.execute(
            "SELECT (SELECT count(*) FROM workbuddy_object_scopes) AS scopes, "
            "(SELECT count(*) FROM workbuddy_object_acl) AS acls"
        ).fetchone()
    assert int(own["scopes"]) >= 1
    assert int(own["acls"]) >= 1

    with workbuddy_transaction(probe, other_ctx) as conn:
        foreign = conn.execute(
            "SELECT (SELECT count(*) FROM workbuddy_object_scopes) AS scopes, "
            "(SELECT count(*) FROM workbuddy_object_acl) AS acls"
        ).fetchone()
    assert int(foreign["scopes"]) == 0
    assert int(foreign["acls"]) == 0

    # And the other tenant cannot resolve access to the object it cannot see.
    other_actor = RbacActor(
        user_id=world["other_owner_user_id"],
        tenant_id=world["other_tenant_id"],
        is_tenant_admin=True,
    )
    decision = RbacService(probe).effective_access(
        other_ctx, object_kind=KIND, object_id=object_id, actor=other_actor
    )
    assert not decision.can_read
    assert decision.sources == ()


def test_the_four_layers_resolve_through_the_service(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    service = RbacService(pool)
    ctx = ctx_for(world)
    owner_actor = actor_for(world, world["owner_user_id"], is_tenant_admin=True)
    member_actor = actor_for(world, world["member_user_id"], department_id=world["department_id"])
    detached_actor = actor_for(world, world["detached_user_id"])

    personal = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=personal,
        scope="personal",
        owner_user_id=world["owner_user_id"],
        actor=owner_actor,
    )

    # Personal: the owner is admin, nobody else is even able to read it.
    assert service.require(
        ctx, object_kind=KIND, object_id=personal, actor=owner_actor, permission="admin"
    ).can_admin
    with pytest.raises(OctopError) as invisible:
        service.require(ctx, object_kind=KIND, object_id=personal, actor=detached_actor)
    assert invisible.value.code is ErrorCode.NOT_FOUND

    # The explicit grant adds read, and nothing more.
    grant_row = service.grant(
        ctx,
        object_kind=KIND,
        object_id=personal,
        actor=owner_actor,
        permission="read",
        user_id=world["member_user_id"],
    )
    assert grant_row["subject"] == {"kind": "user", "id": world["member_user_id"]}
    assert service.require(ctx, object_kind=KIND, object_id=personal, actor=member_actor).can_read
    with pytest.raises(OctopError) as forbidden:
        service.require(
            ctx, object_kind=KIND, object_id=personal, actor=member_actor, permission="write"
        )
    assert forbidden.value.code is ErrorCode.FORBIDDEN
    with pytest.raises(OctopError) as not_an_admin:
        service.grant(
            ctx,
            object_kind=KIND,
            object_id=personal,
            actor=member_actor,
            permission="admin",
            user_id=world["detached_user_id"],
        )
    assert not_an_admin.value.code is ErrorCode.FORBIDDEN

    # Revoking is effective on the next transaction.
    assert service.revoke_grant(
        ctx,
        object_kind=KIND,
        object_id=personal,
        acl_id=grant_row["acl_id"],
        actor=owner_actor,
    ) == {"acl_id": grant_row["acl_id"], "revoked": True}
    with pytest.raises(OctopError) as revoked:
        service.require(ctx, object_kind=KIND, object_id=personal, actor=member_actor)
    assert revoked.value.code is ErrorCode.NOT_FOUND

    # Department: its current members read; a detached member does not.
    department_object = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=department_object,
        scope="department",
        department_id=world["department_id"],
        actor=owner_actor,
    )
    assert service.require(
        ctx, object_kind=KIND, object_id=department_object, actor=member_actor
    ).can_read
    with pytest.raises(OctopError):
        service.require(ctx, object_kind=KIND, object_id=department_object, actor=detached_actor)

    # Department grants reach the department's members too.
    department_grant = service.grant(
        ctx,
        object_kind=KIND,
        object_id=department_object,
        actor=owner_actor,
        permission="write",
        department_id=world["department_id"],
    )
    assert department_grant["subject"] == {"kind": "department", "id": world["department_id"]}
    assert service.require(
        ctx, object_kind=KIND, object_id=department_object, actor=member_actor, permission="write"
    ).can_write

    # A department grant needs a department of this tenant.
    with pytest.raises(OctopError) as foreign_department:
        service.grant(
            ctx,
            object_kind=KIND,
            object_id=department_object,
            actor=owner_actor,
            permission="read",
            department_id=str(uuid.uuid4()),
        )
    assert foreign_department.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT

    # Enterprise: every active member reads.
    enterprise_object = new_object_id()
    service.register_object(
        ctx,
        object_kind=KIND,
        object_id=enterprise_object,
        scope="enterprise",
        actor=owner_actor,
    )
    assert service.require(
        ctx, object_kind=KIND, object_id=enterprise_object, actor=detached_actor
    ).can_read

    # Moving the implicit layer of an existing object needs admin on it.
    with pytest.raises(OctopError) as not_admin:
        service.register_object(
            ctx,
            object_kind=KIND,
            object_id=personal,
            scope="enterprise",
            actor=member_actor,
        )
    assert not_admin.value.code is ErrorCode.NOT_FOUND or (
        not_admin.value.code is ErrorCode.FORBIDDEN
    )


def test_the_sql_visibility_filter_agrees_with_the_resolver(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The crown property: a list query cannot drift from a single-object check."""
    service = RbacService(pool)
    ctx = ctx_for(world)
    owner_actor = actor_for(world, world["owner_user_id"], is_tenant_admin=True)

    layout: dict[str, ScopeGrant] = {
        "personal_owner": ScopeGrant(scope="personal", owner_user_id=world["owner_user_id"]),
        "personal_member": ScopeGrant(scope="personal", owner_user_id=world["member_user_id"]),
        "department_mine": ScopeGrant(scope="department", department_id=world["department_id"]),
        "enterprise": ScopeGrant(scope="enterprise"),
    }
    identifiers: dict[str, str] = {}
    for label, grant in layout.items():
        object_id = new_object_id()
        identifiers[label] = object_id
        service.register_object(
            ctx,
            object_kind=KIND,
            object_id=object_id,
            scope=grant.scope,
            owner_user_id=grant.owner_user_id,
            department_id=grant.department_id,
            actor=owner_actor,
        )

    # One explicit grant, to prove the SQL EXISTS arm as well: the owner may
    # admin their own personal object, so they can hand the member a read.
    service.grant(
        ctx,
        object_kind=KIND,
        object_id=identifiers["personal_owner"],
        actor=owner_actor,
        permission="read",
        user_id=world["member_user_id"],
    )

    actors = {
        "owner": actor_for(world, world["owner_user_id"], department_id=None),
        "member_in_department": actor_for(
            world, world["member_user_id"], department_id=world["department_id"]
        ),
        "detached_member": actor_for(world, world["detached_user_id"], department_id=None),
        "tenant_admin": actor_for(
            world, world["owner_user_id"], department_id=None, is_tenant_admin=True
        ),
    }
    own_ids = set(identifiers.values())
    for label, rbac_actor in actors.items():
        visible_sql = (
            set(service.visible_object_ids(ctx, object_kind=KIND, actor=rbac_actor, limit=100))
            & own_ids
        )
        visible_pure = set()
        for object_id in identifiers.values():
            decision = service.effective_access(
                ctx, object_kind=KIND, object_id=object_id, actor=rbac_actor
            )
            if decision.can_read:
                visible_pure.add(object_id)
        assert visible_sql == visible_pure, label


def test_a_second_object_kind_does_not_leak_into_the_first(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    service = RbacService(pool)
    ctx = ctx_for(world)
    owner_actor = actor_for(world, world["owner_user_id"], is_tenant_admin=True)
    shared_id = new_object_id()
    service.register_object(
        ctx, object_kind="workflow", object_id=shared_id, scope="enterprise", actor=owner_actor
    )

    # The same id under another kind is a different object with no permission row.
    decision = resolve_access(owner_actor, None)
    assert not decision.can_read
    with pytest.raises(OctopError) as not_found:
        service.require(ctx, object_kind="tool", object_id=shared_id, actor=owner_actor)
    assert not_found.value.code is ErrorCode.NOT_FOUND

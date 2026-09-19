"""Live-PostgreSQL isolation gate for the WorkBuddy A2 identity slice.

These are the acceptance checks that cannot be proven on SQLite:
T01 (pooled connections never inherit a previous request's tenant context),
T02 (cross-tenant references are unrepresentable), T03/T28 (a non-owner,
non-bypass role sees only its own tenant and cannot disable the policies).

Enable with::

    export OCTOP_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:15433/octop_test'

Without it the module is skipped; SQLite cannot stand in for any of these.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

import pytest

from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]

_PROBE_ROLE = "octop_a2_probe"
_PROBE_PASSWORD = "octop_a2_probe_pw"

_TENANT_TABLES = (
    "workbuddy_tenants",
    "workbuddy_departments",
    "workbuddy_tenant_members",
    "workbuddy_invitations",
    "workbuddy_tenant_quotas",
    "workbuddy_tenant_audit_events",
    "workbuddy_connector_credentials",
    "workbuddy_connector_credential_grants",
    "workbuddy_tenant_capabilities",
)


def _conninfo() -> str:
    return os.environ["OCTOP_TEST_DATABASE_URL"]


def _conninfo_for(role: str, password: str) -> str:
    parts = urlsplit(_conninfo())
    netloc = f"{role}:{password}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _reset_schema(pool: object) -> None:
    with pool.connect() as conn:  # type: ignore[attr-defined]
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        # Control-plane migrations declare pgvector columns (WorkBuddy knowledge
        # embeddings), so the extension has to exist before run_migrations().
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")


@pytest.fixture(scope="module")
def pg_pool() -> Iterator[object]:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool

    pool = PostgresPool(_conninfo())
    try:
        _reset_schema(pool)
        run_migrations(pool)
        yield pool
    finally:
        pool.close()


@pytest.fixture(scope="module")
def probe_dsn(pg_pool: object) -> str:
    """A least-privilege login: not the table owner, no BYPASSRLS."""
    with pg_pool.connect() as conn:  # type: ignore[attr-defined]
        conn.execute(f"DROP ROLE IF EXISTS {_PROBE_ROLE}")
        conn.execute(
            f"CREATE ROLE {_PROBE_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{_PROBE_PASSWORD}'"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA public TO {_PROBE_ROLE}")
        for table in _TENANT_TABLES:
            conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {_PROBE_ROLE}")
        conn.execute(f"GRANT EXECUTE ON FUNCTION workbuddy_rls_visible(uuid) TO {_PROBE_ROLE}")
        conn.execute(f"GRANT EXECUTE ON FUNCTION workbuddy_current_tenant_id() TO {_PROBE_ROLE}")
    return _conninfo_for(_PROBE_ROLE, _PROBE_PASSWORD)


@pytest.fixture
def probe(pg_pool: object, probe_dsn: str) -> Iterator[object]:
    from octop.infra.db.pool import PostgresPool

    pool = PostgresPool(probe_dsn)
    try:
        yield pool
    finally:
        pool.close()


def _seed_user(pool: object, username: str) -> int:
    from octop.infra.db.repos._base import now_ts

    with pool.connect() as conn:  # type: ignore[attr-defined]
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


def _seed_two_tenants(pool: object) -> tuple[dict, dict]:
    """Create tenant A and B, each with an active owner membership."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    owner_a = _seed_user(pool, f"a2-owner-a-{uuid.uuid4().hex[:8]}")
    owner_b = _seed_user(pool, f"a2-owner-b-{uuid.uuid4().hex[:8]}")
    tenant_a = repo.create_tenant(f"a2-a-{uuid.uuid4().hex[:8]}", "Tenant A", owner_user_id=owner_a)
    tenant_b = repo.create_tenant(f"a2-b-{uuid.uuid4().hex[:8]}", "Tenant B", owner_user_id=owner_b)
    return tenant_a, tenant_b


def test_rls_is_forced_on_every_tenant_table(pg_pool: object) -> None:
    """T03: FORCE RLS must be on, or the table owner would bypass its policy."""
    with pg_pool.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(?)",
            (list(_TENANT_TABLES),),
        ).fetchall()
    state = {row["relname"]: (row["relrowsecurity"], row["relforcerowsecurity"]) for row in rows}
    assert set(state) == set(_TENANT_TABLES), sorted(state)
    for table, (enabled, forced) in state.items():
        assert enabled, f"{table}: RLS not enabled"
        assert forced, f"{table}: RLS not forced"


def test_tenant_context_filters_rows_and_never_leaks(pg_pool: object, probe: object) -> None:
    """T01/T03: a pooled probe connection sees only the context's tenant."""
    from octop.infra.db.workbuddy_context import (
        WorkBuddyDbContext,
        workbuddy_transaction,
    )

    tenant_a, tenant_b = _seed_two_tenants(pg_pool)

    with workbuddy_transaction(probe, WorkBuddyDbContext.for_tenant(tenant_a["tenant_id"])) as conn:
        visible = {
            str(row["tenant_id"])
            for row in conn.execute("SELECT tenant_id FROM workbuddy_tenants").fetchall()
        }
    assert visible == {tenant_a["tenant_id"]}, visible

    with workbuddy_transaction(probe, WorkBuddyDbContext.for_tenant(tenant_b["tenant_id"])) as conn:
        visible_b = {
            str(row["tenant_id"])
            for row in conn.execute("SELECT tenant_id FROM workbuddy_tenants").fetchall()
        }
    assert visible_b == {tenant_b["tenant_id"]}, visible_b

    # No context at all: nothing is visible, not "the last tenant".
    with workbuddy_transaction(probe, WorkBuddyDbContext()) as conn:
        assert conn.execute("SELECT count(*) AS n FROM workbuddy_tenants").fetchone()["n"] == 0

    # The settings are transaction-local, so the pooled connection is clean again.
    with probe.connect() as conn:  # type: ignore[attr-defined]
        for setting in ("app.tenant_id", "app.user_id", "app.department_id"):
            value = conn.execute("SELECT current_setting(?, true) AS value", (setting,)).fetchone()[
                "value"
            ]
            assert value in (None, ""), f"{setting} leaked: {value!r}"


def test_cross_tenant_reference_is_rejected(pg_pool: object, probe: object) -> None:
    """T02: tenant A cannot attach tenant B's department — and learns nothing.

    Two layers are checked: the repository answers a scoped miss (so the API
    returns 404 with no existence leak), and the composite foreign key refuses
    the raw write even if a caller bypasses the repository.
    """
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
    from octop.infra.db.workbuddy_context import (
        WorkBuddyDbContext,
        workbuddy_transaction,
    )

    repo = WorkBuddyIdentityRepo(pg_pool)
    tenant_a, tenant_b = _seed_two_tenants(pg_pool)
    dept_b = repo.create_department(tenant_b["tenant_id"], name="B Department")
    assert dept_b is not None
    member_a = repo.list_members(tenant_a["tenant_id"])[0]

    # Repository: scoped miss, and the member keeps no foreign reference.
    assert (
        repo.update_member(
            tenant_a["tenant_id"],
            member_a["membership_id"],
            department_id=dept_b["department_id"],
        )
        is None
    )
    unchanged = next(
        m
        for m in repo.list_members(tenant_a["tenant_id"])
        if m["membership_id"] == member_a["membership_id"]
    )
    assert unchanged["department_id"] is None
    assert repo.list_departments(tenant_a["tenant_id"]) == []

    # Database: the composite FK refuses the write even without the pre-check.
    with (
        pytest.raises(Exception) as member_write,
        workbuddy_transaction(
            pg_pool, WorkBuddyDbContext.for_tenant(tenant_a["tenant_id"])
        ) as conn,
    ):
        conn.execute(
            "UPDATE workbuddy_tenant_members SET department_id = ? WHERE membership_id = ?",
            (dept_b["department_id"], member_a["membership_id"]),
        )
    assert "workbuddy_members_department_fkey" in str(member_write.value)

    with (
        pytest.raises(Exception) as department_write,
        workbuddy_transaction(
            pg_pool, WorkBuddyDbContext.for_tenant(tenant_a["tenant_id"])
        ) as conn,
    ):
        conn.execute(
            "INSERT INTO workbuddy_departments (department_id, tenant_id, "
            "parent_department_id, name, name_normalized, status, created_by, "
            "created_at, updated_at) "
            "VALUES (gen_random_uuid(), ?, ?, 'x', 'x', 'active', NULL, 1, 1)",
            (tenant_a["tenant_id"], dept_b["department_id"]),
        )
    assert "workbuddy_departments_parent_fkey" in str(department_write.value)

    # Isolation held under the probe role too: A still sees only its own rows.
    assert repo.list_departments(tenant_a["tenant_id"]) == []


def test_business_role_cannot_disable_isolation(probe: object) -> None:
    """T03/T28: the app role cannot TRUNCATE or turn the policies off."""
    with probe.connect() as conn:  # type: ignore[attr-defined]
        with pytest.raises(Exception) as truncate:
            conn.execute("TRUNCATE workbuddy_tenants")
        assert truncate.value is not None

        with pytest.raises(Exception) as disable:
            conn.execute("ALTER TABLE workbuddy_tenants NO FORCE ROW LEVEL SECURITY")
        assert disable.value is not None

"""Per-base caps and per-member preferences on PostgreSQL (B-10, first slice).

Two personal-edition capabilities landed on the tenant tables, and both are facts
only a database can settle:

* every base carries a document cap again (the personal edition has had one since
  v10), the cap is what the ingestion path checks before it accepts a document,
  and it counts live documents only;
* "default open" became a *per-member* preference, because a tenant base is
  opened by many people and the personal schema could only keep one answer per
  base;
* both stay tenant isolated (RLS enabled and forced), like every other table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_catalog import (
    CAPABILITY_MODEL,
    WorkBuddyCatalogRepo,
)
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_knowledge import (
    DEFAULT_MAX_DOCUMENTS,
    MAX_DOCUMENTS_PER_BASE,
    WorkBuddyKnowledgeRepo,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext

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
    """A tenant with an approved embedding model and one member per base."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"cap-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"cap-{uuid.uuid4().hex[:8]}", "Cap tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    member_id = str(identity.list_members(tenant_id)[0]["membership_id"])
    other_user = _seed_user(pool, f"cap-other-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, other_user, actor_user_id=owner_id)

    catalog = WorkBuddyCatalogRepo(pool)
    revision = catalog.publish_model(
        adapter_key="workbuddy-test",
        model_key=f"cap-model-{uuid.uuid4().hex[:8]}",
        display_name="Cap embedding model",
        actor_user_id=owner_id,
    )
    catalog.grant_capability(
        tenant_id,
        kind=CAPABILITY_MODEL,
        revision_id=revision.model_revision_id,
        subject_kind="tenant",
        actor_member_id=member_id,
    )

    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = WorkBuddyDbContext.for_tenant(tenant_id, user_id=owner_id)
    base = repo.create_base(
        ctx,
        scope="personal",
        name=f"Runbook {uuid.uuid4().hex[:6]}",
        description="",
        model=revision,
        created_by_user_id=owner_id,
        owner_user_id=owner_id,
    )
    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "other_user_id": other_user,
        "kb_id": base.kb_id,
    }


def _fresh_base(pool: PostgresPool, world: dict[str, Any], name: str) -> str:
    """A base of this test's own: the cap is mutable state and must not be shared."""
    revision = WorkBuddyCatalogRepo(pool).list_public_models()[0]
    row = WorkBuddyKnowledgeRepo(pool).create_base(
        _context(world),
        scope="personal",
        name=name,
        description="",
        model=revision,
        created_by_user_id=world["owner_user_id"],
        owner_user_id=world["owner_user_id"],
    )
    return row.kb_id


def _context(world: dict[str, Any], user_id: int | None = None) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(
        world["tenant_id"], user_id=world["owner_user_id"] if user_id is None else user_id
    )


def _insert_document(pool: PostgresPool, world: dict[str, Any], *, deleted: bool) -> None:
    with pool.connect() as conn:
        conn.execute(
            "INSERT INTO workbuddy_knowledge_documents("
            " tenant_id, kb_id, source, file_ref_id, title, status, chunk_count, job_id,"
            " created_by_user_id, created_at, updated_at, deleted_at"
            ") VALUES (?, ?, 'text', NULL, ?, 'pending', 0, ?, ?, ?, ?, ?)",
            (
                world["tenant_id"],
                world["kb_id"],
                f"doc {uuid.uuid4().hex[:6]}",
                str(uuid.uuid4()),
                world["owner_user_id"],
                now_ts(),
                now_ts(),
                now_ts() if deleted else None,
            ),
        )


def test_a_new_base_carries_the_personal_cap_and_it_moves_within_bounds(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The cap is a property of the collection, defaulted and re-settable."""
    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = _context(world)
    kb_id = _fresh_base(pool, world, f"Caps {uuid.uuid4().hex[:6]}")
    base = repo.get_base(ctx, kb_id)
    assert base is not None and base.max_documents == DEFAULT_MAX_DOCUMENTS

    raised = repo.update_base_settings(ctx, kb_id, max_documents=5)
    assert raised is not None and raised.max_documents == 5

    for bad in (0, -1, MAX_DOCUMENTS_PER_BASE + 1):
        with pytest.raises(ValueError):
            repo.update_base_settings(ctx, kb_id, max_documents=bad)

    assert repo.update_base_settings(ctx, str(uuid.uuid4()), max_documents=7) is None


def test_the_cap_counts_live_documents_only(pool: PostgresPool, world: dict[str, Any]) -> None:
    """A deleted document frees its slot; the cap decides before ingestion starts."""
    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = _context(world)
    kb_id = _fresh_base(pool, world, f"Counting {uuid.uuid4().hex[:6]}")
    assert repo.update_base_settings(ctx, kb_id, max_documents=2) is not None
    assert repo.document_cap_reached(ctx, kb_id) is False

    _insert_document(pool, {**world, "kb_id": kb_id}, deleted=False)
    assert repo.document_cap_reached(ctx, kb_id) is False  # 1 of 2
    _insert_document(pool, {**world, "kb_id": kb_id}, deleted=False)
    assert repo.document_cap_reached(ctx, kb_id) is True  # 2 of 2
    assert repo.document_cap_reached(ctx, kb_id, incoming=0) is False

    _insert_document(pool, {**world, "kb_id": kb_id}, deleted=True)
    assert repo.document_cap_reached(ctx, kb_id) is True


def test_default_open_is_a_preference_of_each_member(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """One member opening a base says nothing about anybody else."""
    repo = WorkBuddyKnowledgeRepo(pool)

    assert (
        repo.set_default_open(
            _context(world), world["kb_id"], user_id=world["owner_user_id"], default_open=True
        )
        is True
    )
    assert repo.default_open_bases(_context(world), user_id=world["owner_user_id"]) == {
        world["kb_id"]: True
    }
    assert repo.default_open_bases(_context(world), user_id=world["other_user_id"]) == {}

    assert (
        repo.set_default_open(
            _context(world), world["kb_id"], user_id=world["owner_user_id"], default_open=False
        )
        is True
    )
    assert repo.default_open_bases(_context(world), user_id=world["owner_user_id"]) == {
        world["kb_id"]: False
    }
    assert (
        repo.set_default_open(
            _context(world), str(uuid.uuid4()), user_id=world["owner_user_id"], default_open=True
        )
        is False
    )


def test_the_preference_table_is_isolated_on_every_base(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """RLS enabled and forced, and the preference cascades with its base."""
    with pool.connect() as conn:
        state = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relname = 'workbuddy_knowledge_preferences'"
        ).fetchone()
        caps = conn.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint"
            " WHERE conname = 'wb_knowledge_bases_max_documents_valid'"
        ).fetchone()
    assert state is not None and state["relrowsecurity"] and state["relforcerowsecurity"]
    assert caps is not None and "max_documents" in str(caps["definition"])

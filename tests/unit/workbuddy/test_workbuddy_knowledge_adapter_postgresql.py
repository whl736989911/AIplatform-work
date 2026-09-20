"""The personal knowledge API beside the tenant stack (B-12).

The seam only earns its place if it is invisible by default and honest when it is
switched on. Only a database can prove the parts that matter:

* the switch defaults to the personal tables and an unknown value falls back
  there, so a typo can never move a deployment onto tables it has not populated;
* a personal write is mirrored once per base (matching by owner and name is the
  only linkage the personal schema offers), and mirroring again is a no-op;
* a user without a tenant, or a tenant without a granted embedding revision, is
  left alone instead of failing the personal request;
* the projection renders tenant rows in the personal payload shape, including the
  ``shared`` flag the tenant side stores as a scope.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
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
from octop.infra.db.repos.workbuddy_knowledge import WorkBuddyKnowledgeRepo
from octop.infra.workbuddy.knowledge_adapter import (
    KNOWLEDGE_SOURCE_ENV,
    SOURCE_DUAL,
    SOURCE_ENTERPRISE,
    SOURCE_PERSONAL,
    knowledge_source,
    mirror_base,
    mirror_enabled,
    mirror_target,
    project_bases,
    project_enabled,
)

pytestmark = [pytest.mark.postgresql, requires_postgresql]


@dataclass
class _PersonalBase:
    """The fields the adapter reads off a personal-edition base row."""

    name: str
    description: str = ""
    shared: bool = False


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


def _server(pool: PostgresPool) -> Any:
    return type("Server", (), {"services": type("Services", (), {"db": pool})()})()


def _tenant_with_grants(pool: PostgresPool, *, granted: bool) -> dict[str, Any]:
    """A tenant whose owner holds (or does not hold) an approved embedding model."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"adapter-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"adapter-{uuid.uuid4().hex[:8]}", "Adapter tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    member_id = str(identity.list_members(tenant_id)[0]["membership_id"])
    member_user = _seed_user(pool, f"adapter-member-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, member_user, actor_user_id=owner_id)
    model_key = None
    if granted:
        catalog = WorkBuddyCatalogRepo(pool)
        revision = catalog.publish_model(
            adapter_key="workbuddy-test",
            model_key=f"adapter-model-{uuid.uuid4().hex[:8]}",
            display_name="Adapter embedding model",
            actor_user_id=owner_id,
        )
        catalog.grant_capability(
            tenant_id,
            kind=CAPABILITY_MODEL,
            revision_id=revision.model_revision_id,
            subject_kind="tenant",
            actor_member_id=member_id,
        )
        model_key = revision.model_key
    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "owner_member_id": member_id,
        "member_user_id": member_user,
        "model_key": model_key,
    }


# ── the switch ──────────────────────────────────────────────────────────────


def test_the_switch_defaults_to_the_personal_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configuration and an unknown value both mean "serve what exists"."""
    monkeypatch.delenv(KNOWLEDGE_SOURCE_ENV, raising=False)
    assert knowledge_source() == SOURCE_PERSONAL

    monkeypatch.setenv(KNOWLEDGE_SOURCE_ENV, "enterprise")
    assert knowledge_source() == SOURCE_ENTERPRISE
    assert knowledge_source("  dual ") == SOURCE_DUAL

    monkeypatch.setenv(KNOWLEDGE_SOURCE_ENV, "enteprise")  # a typo
    assert knowledge_source() == SOURCE_PERSONAL

    assert mirror_enabled(SOURCE_PERSONAL) is False
    assert mirror_enabled(SOURCE_DUAL) is True
    assert project_enabled(SOURCE_DUAL) is False
    assert project_enabled(SOURCE_ENTERPRISE) is True


# ── the mirror ──────────────────────────────────────────────────────────────


def test_a_personal_base_is_mirrored_once(pool: PostgresPool) -> None:
    """Two writes of the same base keep one tenant row, matched by owner and name."""
    world = _tenant_with_grants(pool, granted=True)
    server = _server(pool)
    base = _PersonalBase(name=f"Handbook {uuid.uuid4().hex[:6]}", description="ops runbook")

    first = mirror_base(server, user_id=world["owner_user_id"], base=base)
    assert first is not None
    second = mirror_base(server, user_id=world["owner_user_id"], base=base)
    assert second == first

    ctx = mirror_target(server, world["owner_user_id"])
    assert ctx is not None
    rows = [row for row in WorkBuddyKnowledgeRepo(pool).list_bases(ctx) if row.name == base.name]
    assert len(rows) == 1
    assert rows[0].kb_id == first
    assert rows[0].scope == SOURCE_PERSONAL
    assert rows[0].owner_user_id == world["owner_user_id"]


def test_a_shared_base_mirrors_as_an_enterprise_scope(pool: PostgresPool) -> None:
    """The personal ``shared`` flag is the tenant side's ``enterprise`` scope."""
    world = _tenant_with_grants(pool, granted=True)
    server = _server(pool)
    base = _PersonalBase(name=f"Shared {uuid.uuid4().hex[:6]}", shared=True)

    mirrored = mirror_base(server, user_id=world["owner_user_id"], base=base)
    assert mirrored is not None

    ctx = mirror_target(server, world["owner_user_id"])
    assert ctx is not None
    row = next(row for row in WorkBuddyKnowledgeRepo(pool).list_bases(ctx) if row.kb_id == mirrored)
    assert row.scope == SOURCE_ENTERPRISE

    # Every tenant member sees a shared base through the projection.
    projected = project_bases(server, user_id=world["member_user_id"], is_admin=False)
    assert projected is not None
    shared = next(item for item in projected if item["knowledge_base_id"] == mirrored)
    assert shared["shared"] is True
    assert shared["doc_count"] == 0
    assert shared["max_documents"] > 0


def test_a_user_without_a_tenant_is_left_alone(pool: PostgresPool) -> None:
    """Standalone users keep the personal tables: no tenant, no mirror, no error."""
    stranger = _seed_user(pool, f"adapter-stranger-{uuid.uuid4().hex[:8]}")
    server = _server(pool)
    assert mirror_target(server, stranger) is None
    assert mirror_base(server, user_id=stranger, base=_PersonalBase(name="Personal only")) is None
    assert project_bases(server, user_id=stranger, is_admin=False) is None


def test_a_tenant_without_a_granted_model_is_left_alone(pool: PostgresPool) -> None:
    """Mirroring needs a usable embedding revision; without one nothing is written."""
    world = _tenant_with_grants(pool, granted=False)
    server = _server(pool)
    name = f"Ungranted {uuid.uuid4().hex[:6]}"
    assert (
        mirror_base(server, user_id=world["owner_user_id"], base=_PersonalBase(name=name)) is None
    )

    ctx = mirror_target(server, world["owner_user_id"])
    assert ctx is not None
    assert [row for row in WorkBuddyKnowledgeRepo(pool).list_bases(ctx) if row.name == name] == []


def test_the_projection_answers_in_the_personal_shape(pool: PostgresPool) -> None:
    """The personal page must not be able to tell which side answered."""
    world = _tenant_with_grants(pool, granted=True)
    server = _server(pool)
    name = f"Projected {uuid.uuid4().hex[:6]}"
    mirrored = mirror_base(server, user_id=world["owner_user_id"], base=_PersonalBase(name=name))
    assert mirrored is not None

    projected = project_bases(server, user_id=world["owner_user_id"], is_admin=False)
    assert projected is not None
    item = next(entry for entry in projected if entry["knowledge_base_id"] == mirrored)
    assert set(item) == {
        "id",
        "knowledge_base_id",
        "owner_user_id",
        "name",
        "description",
        "default_open",
        "shared",
        "icon_name",
        "embedding_model",
        "embedding_dim",
        "doc_count",
        "max_documents",
        "created_at",
        "updated_at",
    }
    assert item["id"] == mirrored
    assert item["name"] == name
    assert item["shared"] is False
    assert item["embedding_model"] == world["model_key"]
    assert item["embedding_dim"] > 0

"""Document folders on PostgreSQL (B-10).

The personal edition modelled a folder as a placeholder document; the tenant side
takes the path on the document itself. Only a database can prove the parts that
matter:

* a folder exists exactly as long as it holds a live document, and the listing
  reports the root even when it is empty;
* moving a document between folders is a single write, and moving it back to the
  root is the empty path;
* the shape the service refuses (absolute, traversal, doubled or trailing
  separators, stray spaces) is the shape the schema refuses too, so a raw write
  cannot create a path the listing would have to interpret;
* the document payload carries the folder, and a member without write permission
  sees the same 404 as a stranger.
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
from octop.infra.db.repos.workbuddy_knowledge import WorkBuddyKnowledgeRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import OctopError
from octop.infra.workbuddy.knowledge import (
    WorkBuddyKnowledgeActor,
    WorkBuddyKnowledgeService,
    normalize_folder_path,
)

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
    """A tenant with an approved model, one personal base and two documents."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"fold-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"fold-{uuid.uuid4().hex[:8]}", "Folder tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member = str(identity.list_members(tenant_id)[0]["membership_id"])
    other_id = _seed_user(pool, f"fold-other-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, other_id, actor_user_id=owner_id)

    catalog = WorkBuddyCatalogRepo(pool)
    revision = catalog.publish_model(
        adapter_key="ollama",
        model_key="bge-m3",
        display_name="Stub embedding",
        actor_user_id=owner_id,
    )
    catalog.grant_capability(
        tenant_id,
        kind=CAPABILITY_MODEL,
        revision_id=revision.model_revision_id,
        subject_kind="tenant",
        actor_member_id=owner_member,
    )
    repo = WorkBuddyKnowledgeRepo(pool)
    base = repo.create_base(
        WorkBuddyDbContext.for_tenant(tenant_id, user_id=owner_id),
        scope="personal",
        name="Folder base",
        description="",
        model=revision,
        created_by_user_id=owner_id,
        owner_user_id=owner_id,
    )
    documents = [str(uuid.uuid4()), str(uuid.uuid4())]
    with pool.connect() as conn, conn.transaction():
        for index, document_id in enumerate(documents):
            conn.execute(
                "INSERT INTO workbuddy_knowledge_documents("
                " tenant_id, document_id, kb_id, source, file_ref_id, title, status,"
                " chunk_count, job_id, created_by_user_id, created_at, updated_at"
                ") VALUES (?, ?, ?, 'text', NULL, ?, 'pending', 0, ?, ?, ?, ?)",
                (
                    tenant_id,
                    document_id,
                    base.kb_id,
                    f"Doc {index}",
                    str(uuid.uuid4()),
                    owner_id,
                    now_ts(),
                    now_ts(),
                ),
            )
    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "other_user_id": other_id,
        "kb_id": base.kb_id,
        "documents": documents,
    }


def _service(pool: PostgresPool) -> WorkBuddyKnowledgeService:
    return WorkBuddyKnowledgeService(db=pool)


def _actor(world: dict[str, Any], user_id: int | None = None) -> WorkBuddyKnowledgeActor:
    return WorkBuddyKnowledgeActor(
        user_id=world["owner_user_id"] if user_id is None else user_id,
        tenant_id=world["tenant_id"],
        department_id=None,
    )


def test_folders_are_created_by_moving_a_document(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The listing is the distinct paths of live documents; the root is always present."""
    service = _service(pool)
    listed = service.list_folders(_actor(world), world["kb_id"])
    assert listed["folders"] == [{"path": "", "document_count": 2}]

    service.move_document(
        _actor(world), world["kb_id"], world["documents"][0], folder_path="Ops/Runbooks"
    )
    after = service.list_folders(_actor(world), world["kb_id"])
    assert after["folders"] == [
        {"path": "", "document_count": 1},
        {"path": "Ops/Runbooks", "document_count": 1},
    ]

    moved = service.move_document(
        _actor(world), world["kb_id"], world["documents"][0], folder_path=""
    )
    assert moved["folder_path"] == ""
    assert service.list_folders(_actor(world), world["kb_id"])["folders"] == [
        {"path": "", "document_count": 2}
    ]


def test_the_document_payload_carries_its_folder(pool: PostgresPool, world: dict[str, Any]) -> None:
    """A client renders the tree from the documents it already lists."""
    service = _service(pool)
    service.move_document(_actor(world), world["kb_id"], world["documents"][1], folder_path="Ops")
    payloads = service.list_documents(_actor(world), world["kb_id"])
    by_id = {item["document_id"]: item for item in payloads}
    assert by_id[world["documents"][1]]["folder_path"] == "Ops"
    assert by_id[world["documents"][0]]["folder_path"] == ""


@pytest.mark.parametrize(
    "candidate",
    ["/absolute", "trailing/", "doubled//path", "..", "a/../b", "a\\b"],
)
def test_the_service_and_the_schema_refuse_the_same_paths(
    pool: PostgresPool, world: dict[str, Any], candidate: str
) -> None:
    """A path the listing could not navigate never reaches a row."""
    service = _service(pool)
    with pytest.raises(OctopError) as refused:
        service.move_document(
            _actor(world), world["kb_id"], world["documents"][0], folder_path=candidate
        )
    assert refused.value.code.value == "WORKBUDDY_INVALID_ARGUMENT"

    with pool.connect() as conn, pytest.raises(Exception) as raw:
        conn.execute(
            "UPDATE workbuddy_knowledge_documents SET folder_path = ?"
            " WHERE tenant_id = ? AND document_id = ?",
            (candidate, world["tenant_id"], world["documents"][0]),
        )
    assert "folder_path" in str(raw.value)


def test_paths_are_canonicalised_not_guessed() -> None:
    """The normaliser accepts what it can prove and refuses the rest."""
    assert normalize_folder_path(None) == ""
    assert normalize_folder_path("  ") == ""
    assert normalize_folder_path("Ops/Runbooks") == "Ops/Runbooks"
    # Surrounding whitespace is canonicalised away; whitespace *inside* a segment
    # is refused rather than silently trimmed, so the stored path is what was asked.
    assert normalize_folder_path("  Ops  ") == "Ops"
    with pytest.raises(OctopError):
        normalize_folder_path("Ops / Runbooks")
    for bad in ("/x", "x/", "x//y", "..", "a/./b", "a/../b"):
        with pytest.raises(OctopError):
            normalize_folder_path(bad)


def test_a_member_without_write_permission_sees_the_uniform_404(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """A personal base is invisible to other members, folders included."""
    service = _service(pool)
    other = _actor(world, user_id=world["other_user_id"])
    with pytest.raises(OctopError) as listed:
        service.list_folders(other, world["kb_id"])
    assert listed.value.code.value == "NOT_FOUND"
    with pytest.raises(OctopError) as moved:
        service.move_document(other, world["kb_id"], world["documents"][0], folder_path="Ops")
    assert moved.value.code.value == "NOT_FOUND"

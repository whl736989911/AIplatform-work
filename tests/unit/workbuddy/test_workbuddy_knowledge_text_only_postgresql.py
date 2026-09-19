"""The text-only document channel at the database layer (migration 051).

Only a database can prove what this migration is about:

* a document may exist without a ``file_ref_id`` — that is what a migrated
  document is, because the personal edition kept chunk text and vectors and no
  original file — while an uploaded document still must name its file reference;
* the unique index that allows one document per stored file still holds, and it
  no longer forbids a second text-only document in the same base (which a
  synthetic file reference would have);
* the discriminator is a closed vocabulary, so a misspelled source cannot quietly
  become the migration channel.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``); the
database is dedicated to the suite and is reset here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_knowledge import (
    WorkBuddyKnowledgeRepo,
    WorkBuddyPlatformModelRevisionRow,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext

pytestmark = [pytest.mark.postgresql, requires_postgresql]

MODEL_REVISION_ID = "cccccccc-0000-4000-8000-000000000001"
CHECKSUM = "a" * 64


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
            pytest.skip("pgvector is required by the knowledge migrations")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture(scope="module")
def world(pool: PostgresPool) -> dict[str, Any]:
    """One tenant with a member and one knowledge base to hang documents off."""
    identity = WorkBuddyIdentityRepo(pool)
    with pool.connect() as conn:
        user_id = int(
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, 'hash', 'user', ?) RETURNING id",
                (f"kb-text-{uuid.uuid4().hex[:8]}", now_ts()),
            ).fetchone()["id"]
        )
    tenant = identity.create_tenant(
        f"kb-text-{uuid.uuid4().hex[:8]}", "Text-only tenant", owner_user_id=user_id
    )
    tenant_id = str(tenant["tenant_id"])
    ctx = WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id)
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_platform_model_revisions "
            "(model_revision_id, adapter_key, model_key, revision, display_name, status, "
            "published_by_user_id, published_at) "
            "VALUES (?, 'ollama', 'bge-m3', 1, 'Test embedding', 'published', ?, ?)",
            (MODEL_REVISION_ID, user_id, now_ts()),
        )
    base = WorkBuddyKnowledgeRepo(pool).create_base(
        ctx,
        scope="personal",
        name="Migrated base",
        description="",
        model=WorkBuddyPlatformModelRevisionRow(
            model_revision_id=MODEL_REVISION_ID,
            adapter_key="ollama",
            model_key="bge-m3",
            revision=1,
            display_name="bge-m3 r1",
            status="published",
        ),
        created_by_user_id=user_id,
        owner_user_id=user_id,
    )
    return {"tenant_id": tenant_id, "user_id": user_id, "kb_id": base.kb_id, "ctx": ctx}


@pytest.fixture(scope="module")
def stored_file(pool: PostgresPool, world: dict[str, Any]) -> str:
    """A real completed upload and its file reference, for the upload-side checks."""
    file_ref_id = str(uuid.uuid4())
    upload_id = str(uuid.uuid4())
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_knowledge_uploads ("
            "tenant_id, upload_id, kb_id, requested_by_user_id, filename, mime_type, "
            "size_bytes, object_key, status, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, 'doc.md', 'text/markdown', 5, 'objects/doc.md', "
            "'completed', ?, ?)",
            (
                world["tenant_id"],
                upload_id,
                world["kb_id"],
                world["user_id"],
                now_ts(),
                now_ts() + 900,
            ),
        )
        conn.execute(
            "INSERT INTO workbuddy_knowledge_file_refs ("
            "tenant_id, file_ref_id, kb_id, upload_id, object_key, filename, mime_type, "
            "size_bytes, checksum_sha256, created_by_user_id, created_at) "
            "VALUES (?, ?, ?, ?, 'objects/doc.md', 'doc.md', 'text/markdown', 5, ?, ?, ?)",
            (
                world["tenant_id"],
                file_ref_id,
                world["kb_id"],
                upload_id,
                CHECKSUM,
                world["user_id"],
                now_ts(),
            ),
        )
    return file_ref_id


def _insert_document(
    pool: PostgresPool,
    world: dict[str, Any],
    *,
    source: str | None,
    file_ref_id: str | None,
) -> None:
    """Insert a document row directly, bypassing the service's own validation."""
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_knowledge_documents ("
            "tenant_id, document_id, kb_id, file_ref_id, source, title, status, job_id, "
            "created_by_user_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, coalesce(?, 'upload'), 'migrated', 'pending', ?, ?, ?, ?)",
            (
                world["tenant_id"],
                str(uuid.uuid4()),
                world["kb_id"],
                file_ref_id,
                source,
                str(uuid.uuid4()),
                world["user_id"],
                now_ts(),
                now_ts(),
            ),
        )


def test_migration_documents_need_no_file_reference(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    knowledge = WorkBuddyKnowledgeRepo(pool)
    document = knowledge.create_document(
        world["ctx"],
        world["kb_id"],
        file_ref_id=None,
        source="migration",
        title="migrated policy",
        created_by_user_id=world["user_id"],
        job_id=str(uuid.uuid4()),
    )
    assert document.file_ref_id is None
    assert document.source == "migration"

    # Two of them in the same base: a synthetic placeholder would have collided.
    second = knowledge.create_document(
        world["ctx"],
        world["kb_id"],
        file_ref_id=None,
        source="migration",
        title="migrated appendix",
        created_by_user_id=world["user_id"],
        job_id=str(uuid.uuid4()),
    )
    assert second.document_id != document.document_id


def test_an_uploaded_document_still_needs_its_file_reference(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_document(pool, world, source="upload", file_ref_id=None)


def test_a_text_only_document_may_not_name_a_file_reference(
    pool: PostgresPool, world: dict[str, Any], stored_file: str
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_document(pool, world, source="migration", file_ref_id=stored_file)


def test_the_source_vocabulary_is_closed(pool: PostgresPool, world: dict[str, Any]) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_document(pool, world, source="telepathy", file_ref_id=None)


def test_the_source_default_keeps_the_old_contract(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """A row that omits the source is an upload, so it must carry a file reference."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_document(pool, world, source=None, file_ref_id=None)


def test_one_document_per_stored_file_still_holds(
    pool: PostgresPool, world: dict[str, Any], stored_file: str
) -> None:
    knowledge = WorkBuddyKnowledgeRepo(pool)
    first = knowledge.create_document(
        world["ctx"],
        world["kb_id"],
        file_ref_id=stored_file,
        source="upload",
        title="doc.md",
        created_by_user_id=world["user_id"],
        job_id=str(uuid.uuid4()),
    )
    assert first.file_ref_id == stored_file

    with pytest.raises(psycopg.errors.UniqueViolation):
        knowledge.create_document(
            world["ctx"],
            world["kb_id"],
            file_ref_id=stored_file,
            source="upload",
            title="doc again",
            created_by_user_id=world["user_id"],
            job_id=str(uuid.uuid4()),
        )

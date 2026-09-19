"""Retrieval guarantees at the SQL layer: one base, one readable generation, one tenant.

The service-level suite drives a fake repository, so the query that actually
decides what a search may return is never exercised there. These checks run the
real ``search_chunks`` statement against PostgreSQL and pin the three properties
the WorkBuddy knowledge contract depends on:

* a generation that is not the document's active generation is not retrievable,
  even when it is itself ``ready`` (publishing, not indexing, is what exposes
  content);
* a generation that never reached ``ready`` is not retrievable;
* retrieval never crosses a knowledge base and never crosses a tenant, and the
  stored embedding dimension is fixed at 1024.

Gated on ``OCTOP_TEST_DATABASE_URL`` (see ``tests/support/postgresql.py``); the
database is dedicated to the suite and is reset here.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from tests.support.postgresql import requires_postgresql

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.workbuddy_knowledge import (
    WorkBuddyKnowledgeRepo,
    vector_literal,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction

pytestmark = [pytest.mark.postgresql, requires_postgresql]

DIMENSIONS = 1024
ADAPTER_KEY = "octop.test.embedding"
MODEL_KEY = "test-embedding"


def _embedding(seed: float) -> list[float]:
    """A deterministic 1024-dimension unit-ish vector."""
    vector = [0.0] * DIMENSIONS
    vector[0] = seed
    vector[1] = 1.0
    return vector


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


def _corpus(pool: PostgresPool) -> dict[str, str]:
    """One tenant with two knowledge bases; base A holds three generations of one document."""
    now = int(time.time())
    suffix = uuid.uuid4().hex[:8]
    # Platform model revisions are unique per (adapter, model, revision), so each
    # corpus pins its own: tests must not depend on each other's rows.
    model_key = f"{MODEL_KEY}-{suffix}"
    ids = {
        "tenant_a": str(uuid.uuid4()),
        "tenant_b": str(uuid.uuid4()),
        "kb_a": str(uuid.uuid4()),
        "kb_b": str(uuid.uuid4()),
        "document_a": str(uuid.uuid4()),
        "generation_active": str(uuid.uuid4()),
        "generation_unpublished": str(uuid.uuid4()),
        "generation_ready_inactive": str(uuid.uuid4()),
    }

    with workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn:
        for tenant_id, slug in (
            (ids["tenant_a"], f"kb-a-{suffix}"),
            (ids["tenant_b"], f"kb-b-{suffix}"),
        ):
            conn.execute(
                """
                INSERT INTO workbuddy_tenants
                  (tenant_id, slug, slug_normalized, name, status, created_at, updated_at)
                VALUES (?, ?, ?, 'Knowledge Tenant', 'active', ?, ?)
                """,
                (tenant_id, slug, slug, now, now),
            )
        user_id = int(
            conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, 'not-a-real-hash', 'user', ?) RETURNING id
                """,
                (f"wb-knowledge-{suffix}", now),
            ).fetchone()["id"]
        )
        revision_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO workbuddy_platform_model_revisions
              (model_revision_id, adapter_key, model_key, revision, display_name, status,
               published_by_user_id, published_at)
            VALUES (?, ?, ?, 1, 'Test embedding', 'published', ?, ?)
            """,
            (revision_id, ADAPTER_KEY, model_key, user_id, now),
        )
        for kb_id, tenant_id, name in (
            (ids["kb_a"], ids["tenant_a"], "Base A"),
            (ids["kb_b"], ids["tenant_a"], "Base B"),
        ):
            conn.execute(
                """
                INSERT INTO workbuddy_knowledge_bases
                  (tenant_id, kb_id, scope, name, description, embedding_model_revision_id,
                   embedding_adapter_key, embedding_model_key, embedding_revision,
                   embedding_dimensions, created_by_user_id, created_at, updated_at)
                VALUES (?, ?, 'enterprise', ?, '', ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    kb_id,
                    name,
                    revision_id,
                    ADAPTER_KEY,
                    model_key,
                    DIMENSIONS,
                    user_id,
                    now,
                    now,
                ),
            )

        upload_id = str(uuid.uuid4())
        file_ref_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO workbuddy_knowledge_uploads
              (tenant_id, upload_id, kb_id, requested_by_user_id, filename, mime_type,
               size_bytes, object_key, status, created_at, expires_at, completed_at)
            VALUES (?, ?, ?, ?, 'policy.txt', 'text/plain', 11, ?, 'completed', ?, ?, ?)
            """,
            (
                ids["tenant_a"],
                upload_id,
                ids["kb_a"],
                user_id,
                f"{suffix}/policy.txt",
                now,
                now + 3600,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO workbuddy_knowledge_file_refs
              (tenant_id, file_ref_id, kb_id, upload_id, object_key, filename, mime_type,
               size_bytes, checksum_sha256, created_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?, 'policy.txt', 'text/plain', 11, ?, ?, ?)
            """,
            (
                ids["tenant_a"],
                file_ref_id,
                ids["kb_a"],
                upload_id,
                f"{suffix}/policy.txt",
                "a" * 64,
                user_id,
                now,
            ),
        )
        # documents.active_generation_id and generations.document_id reference each
        # other, so the document starts as pending without an active generation,
        # the generations are inserted, and the active marker is set afterwards.
        conn.execute(
            """
            INSERT INTO workbuddy_knowledge_documents
              (tenant_id, document_id, kb_id, file_ref_id, title, status,
               active_generation_id, chunk_count, job_id, created_by_user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'Policy', 'pending', NULL, 0, ?, ?, ?, ?)
            """,
            (
                ids["tenant_a"],
                ids["document_a"],
                ids["kb_a"],
                file_ref_id,
                str(uuid.uuid4()),
                user_id,
                now,
                now,
            ),
        )

        # Generations come next: the document's active marker points at one.
        generations = (
            (ids["generation_active"], "ready", f"{suffix}-active"),
            (ids["generation_ready_inactive"], "ready", f"{suffix}-stale"),
            (ids["generation_unpublished"], "building", f"{suffix}-unpublished"),
        )
        for index, (generation_id, status, _content) in enumerate(generations, start=1):
            conn.execute(
                """
                INSERT INTO workbuddy_knowledge_generations
                  (tenant_id, generation_id, kb_id, document_id, generation_number, status,
                   embedding_model_revision_id, embedding_adapter_key, embedding_model_key,
                   embedding_revision, embedding_dimensions, chunk_count, created_by_user_id,
                   created_at, ready_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?, ?, ?)
                """,
                (
                    ids["tenant_a"],
                    generation_id,
                    ids["kb_a"],
                    ids["document_a"],
                    index,
                    status,
                    revision_id,
                    ADAPTER_KEY,
                    model_key,
                    DIMENSIONS,
                    user_id,
                    now,
                    now if status == "ready" else None,
                ),
            )

        conn.execute(
            """
            UPDATE workbuddy_knowledge_documents
               SET status = 'ready', active_generation_id = ?, chunk_count = 1
             WHERE tenant_id = ? AND document_id = ?
            """,
            (ids["generation_active"], ids["tenant_a"], ids["document_a"]),
        )

        for _generation_id, _status, content in generations:
            conn.execute(
                """
                INSERT INTO workbuddy_knowledge_chunks
                  (tenant_id, chunk_id, kb_id, document_id, generation_id, ordinal,
                   content, token_count, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, 3, ?::vector, ?)
                """,
                (
                    ids["tenant_a"],
                    str(uuid.uuid4()),
                    ids["kb_a"],
                    ids["document_a"],
                    _generation_id,
                    content,
                    vector_literal(_embedding(0.5)),
                    now,
                ),
            )

    ids["user_id"] = str(user_id)
    return ids


def test_only_the_active_generation_of_a_ready_document_is_retrievable(
    pool: PostgresPool,
) -> None:
    ids = _corpus(pool)
    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = WorkBuddyDbContext.for_tenant(ids["tenant_a"], user_id=int(ids["user_id"]))

    hits = repo.search_chunks(ctx, ids["kb_a"], query_vector=_embedding(0.5), limit=10)
    assert [hit.generation_id for hit in hits] == [ids["generation_active"]]
    assert hits[0].content.endswith("-active")

    # The stale-but-ready generation becomes visible only once it is the active
    # one: publishing is what exposes content, not finishing an index run.
    with workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn:
        conn.execute(
            "UPDATE workbuddy_knowledge_documents SET active_generation_id = ? WHERE document_id = ?",
            (ids["generation_ready_inactive"], ids["document_a"]),
        )
    published = repo.search_chunks(ctx, ids["kb_a"], query_vector=_embedding(0.5), limit=10)
    assert [hit.generation_id for hit in published] == [ids["generation_ready_inactive"]]


def test_an_active_marker_on_a_generation_that_never_became_ready_exposes_nothing(
    pool: PostgresPool,
) -> None:
    ids = _corpus(pool)
    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = WorkBuddyDbContext.for_tenant(ids["tenant_a"], user_id=int(ids["user_id"]))

    with workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn:
        conn.execute(
            "UPDATE workbuddy_knowledge_documents SET active_generation_id = ? WHERE document_id = ?",
            (ids["generation_unpublished"], ids["document_a"]),
        )

    # Pointing the active marker at a generation the indexer never published
    # yields nothing: the statement requires status = 'ready' as well.
    assert repo.search_chunks(ctx, ids["kb_a"], query_vector=_embedding(0.5), limit=10) == []


def test_retrieval_never_crosses_a_base_or_a_tenant(pool: PostgresPool) -> None:
    ids = _corpus(pool)
    repo = WorkBuddyKnowledgeRepo(pool)

    # Base B belongs to the same tenant but has no chunks: searching for base A's
    # vector there must return nothing.
    assert (
        repo.search_chunks(
            WorkBuddyDbContext.for_tenant(ids["tenant_a"], user_id=int(ids["user_id"])),
            ids["kb_b"],
            query_vector=_embedding(0.5),
            limit=10,
        )
        == []
    )

    # Another tenant asking for the same base id sees nothing, RLS included.
    foreign = repo.search_chunks(
        WorkBuddyDbContext.for_tenant(ids["tenant_b"]),
        ids["kb_a"],
        query_vector=_embedding(0.5),
        limit=10,
    )
    assert foreign == []


def test_stored_embeddings_are_pinned_to_1024_dimensions(pool: PostgresPool) -> None:
    ids = _corpus(pool)

    with (
        pytest.raises(psycopg.errors.DataError),
        workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn,
    ):
        conn.execute(
            """
            INSERT INTO workbuddy_knowledge_chunks
              (tenant_id, chunk_id, kb_id, document_id, generation_id, ordinal,
               content, token_count, embedding, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 'dimension mismatch', 3, ?::vector, ?)
            """,
            (
                ids["tenant_a"],
                str(uuid.uuid4()),
                ids["kb_a"],
                ids["document_a"],
                ids["generation_active"],
                vector_literal([0.25, 0.25, 0.25]),
                int(time.time()),
            ),
        )

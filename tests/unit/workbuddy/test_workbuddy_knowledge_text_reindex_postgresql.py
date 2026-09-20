"""Document text read-back and reindexing on PostgreSQL (B-10).

The personal edition could show a document's text and rebuild its index; the
tenant side stored the same chunks but never read them back. Only a database can
prove the parts that matter:

* a text-only document's text comes back in chunk order, and ``limit`` truncates
  it for the preview while the export keeps the whole thing;
* reindexing one document publishes a fresh generation and keeps the text
  readable, and reindexing the base reaches every live document;
* a member without access to a personal base sees the same 404 as a stranger.

The service is exercised against the real repository with a stub embedder, so the
generations, chunks and job rows under test are real rows.
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
    EMBEDDING_DIMENSIONS,
    EmbeddingDescriptor,
    KnowledgeHooks,
    WorkBuddyKnowledgeActor,
    WorkBuddyKnowledgeService,
    configure_workbuddy_knowledge_hooks,
    reset_workbuddy_knowledge_hooks,
)

pytestmark = [pytest.mark.postgresql, requires_postgresql]

TEXT = "alpha beta gamma\n\ndelta epsilon zeta"


class _StubEmbedder:
    """A deterministic embedder: the indexing path needs vectors, not a provider."""

    def __init__(self, *, adapter_key: str, model_key: str, revision: int) -> None:
        self._descriptor = EmbeddingDescriptor(
            adapter_key=adapter_key,
            model_key=model_key,
            revision=revision,
            dimensions=EMBEDDING_DIMENSIONS,
        )

    def available(self) -> bool:
        return True

    def describe(self) -> EmbeddingDescriptor:
        return self._descriptor

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.05] * EMBEDDING_DIMENSIONS for _ in texts]


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
    """A tenant, an approved embedding model, one personal base and two members."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"text-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"text-{uuid.uuid4().hex[:8]}", "Text tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member = str(identity.list_members(tenant_id)[0]["membership_id"])
    other_id = _seed_user(pool, f"text-other-{uuid.uuid4().hex[:8]}")
    identity.add_membership(tenant_id, other_id, actor_user_id=owner_id)

    catalog = WorkBuddyCatalogRepo(pool)
    revision = catalog.publish_model(
        adapter_key="ollama",
        model_key="bge-m3",  # the pinned knowledge-base embedder
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
        name="Text base",
        description="",
        model=revision,
        created_by_user_id=owner_id,
        owner_user_id=owner_id,
    )
    document_id = str(uuid.uuid4())
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_knowledge_documents("
            " tenant_id, document_id, kb_id, source, file_ref_id, title, status,"
            " chunk_count, job_id, created_by_user_id, created_at, updated_at"
            ") VALUES (?, ?, ?, 'text', NULL, 'Runbook', 'pending', 0, ?, ?, ?, ?)",
            (
                tenant_id,
                document_id,
                base.kb_id,
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
        "document_id": document_id,
        "model_key": revision.model_key,
        "model_revision": revision.revision,
    }


def _service(pool: PostgresPool, world: dict[str, Any]) -> WorkBuddyKnowledgeService:
    configure_workbuddy_knowledge_hooks(
        KnowledgeHooks(
            embedder=_StubEmbedder(
                adapter_key="ollama",
                model_key=world["model_key"],
                revision=world["model_revision"],
            )
        )
    )
    return WorkBuddyKnowledgeService(db=pool)


def _actor(world: dict[str, Any], user_id: int | None = None) -> WorkBuddyKnowledgeActor:
    return WorkBuddyKnowledgeActor(
        user_id=world["owner_user_id"] if user_id is None else user_id,
        tenant_id=world["tenant_id"],
        department_id=None,
    )


def _index(pool: PostgresPool, world: dict[str, Any]) -> WorkBuddyKnowledgeService:
    service = _service(pool, world)
    service.index_text_document(
        tenant_id=world["tenant_id"],
        actor_user_id=world["owner_user_id"],
        kb_id=world["kb_id"],
        document_id=world["document_id"],
        text=TEXT,
    )
    return service


def test_the_indexed_text_comes_back_in_order_and_limit_previews(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The chunks are the document: the read-back is the text, and ``limit`` cuts it."""
    service = _index(pool, world)
    try:
        whole = service.document_text(_actor(world), world["kb_id"], world["document_id"])
        assert whole["text"] == TEXT
        assert whole["truncated"] is False
        assert whole["chunk_count"] >= 1

        preview = service.document_text(
            _actor(world), world["kb_id"], world["document_id"], limit=5
        )
        assert preview["text"] == TEXT[:5]
        assert preview["truncated"] is True
    finally:
        reset_workbuddy_knowledge_hooks()


def test_reindexing_one_document_publishes_a_new_generation(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The document stays readable while its index is rebuilt."""
    service = _index(pool, world)
    repo = WorkBuddyKnowledgeRepo(pool)
    ctx = WorkBuddyDbContext.for_tenant(world["tenant_id"], user_id=world["owner_user_id"])
    before = repo.get_document(ctx, world["kb_id"], world["document_id"])
    assert before is not None and before.active_generation_id is not None
    try:
        result = service.reindex_document(_actor(world), world["kb_id"], world["document_id"])
        assert result["reindexed"] is True
        assert result["job_id"] == before.job_id

        after = repo.get_document(ctx, world["kb_id"], world["document_id"])
        assert after is not None
        assert after.active_generation_id != before.active_generation_id
        assert (
            service.document_text(_actor(world), world["kb_id"], world["document_id"])["text"]
            == TEXT
        )
    finally:
        reset_workbuddy_knowledge_hooks()


def test_reindexing_the_base_reaches_every_live_document(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """A whole-base rebuild reports what it reached and nothing else."""
    service = _index(pool, world)
    try:
        result = service.reindex_base(_actor(world), world["kb_id"])
        assert result["kb_id"] == world["kb_id"]
        assert result["queued"] >= 1
        assert result["failed"] == []
    finally:
        reset_workbuddy_knowledge_hooks()


def test_a_member_without_access_sees_the_uniform_404(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """A personal base is invisible to other members, text included."""
    service = _index(pool, world)
    try:
        with pytest.raises(OctopError) as refusal:
            service.document_text(
                _actor(world, user_id=world["other_user_id"]),
                world["kb_id"],
                world["document_id"],
            )
        assert refusal.value.code.value == "NOT_FOUND"
        with pytest.raises(OctopError) as reindex_refusal:
            service.reindex_base(_actor(world, user_id=world["other_user_id"]), world["kb_id"])
        assert reindex_refusal.value.code.value == "NOT_FOUND"
    finally:
        reset_workbuddy_knowledge_hooks()

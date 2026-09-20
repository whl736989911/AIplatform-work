"""Local ONNX embedding revisions on PostgreSQL (B-11).

A local ONNX embedding model is one *kind* of platform model revision: the same
catalog, the same immutable revision rows, the same subject grants. Only a
database can prove the parts that matter:

* a published ONNX revision reads back the declaration it was published with,
  and a hosted revision keeps both fields empty;
* an authorized ONNX revision can back a knowledge base, and ``bge-m3`` still
  can — the widening did not move the hosted path;
* a declared width other than the storage layer's fixed vector width is refused
  with the code every unpinnable model already gets, and an ungranted revision
  is refused like any other unapproved model;
* the schema refuses an ``onnx`` revision that cannot say what it produces or
  which local model it loads, including through a raw write;
* revocation keeps the row (status and revocation stamp change, nothing is
  deleted), so the catalog stays auditable.

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
from octop.infra.db.repos.workbuddy_catalog import (
    ADAPTER_KEY_ONNX,
    CAPABILITY_MODEL,
    WorkBuddyCatalogRepo,
    WorkBuddyInvalidInput,
)
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.knowledge import (
    VECTOR_DIMENSIONS,
    WorkBuddyKnowledgeActor,
    WorkBuddyKnowledgeService,
)

pytestmark = [pytest.mark.postgresql, requires_postgresql]

LOCAL_MODEL_KEY = "BAAI/bge-small-zh-v1.5"
NARROW_LOCAL_MODEL_KEY = "jinaai/jina-embeddings-v2-base-zh"


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
    """One tenant granted a local ONNX revision, bge-m3, and a too-narrow local model."""
    identity = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"onnx-owner-{uuid.uuid4().hex[:8]}")
    tenant = identity.create_tenant(
        f"onnx-{uuid.uuid4().hex[:8]}", "ONNX tenant", owner_user_id=owner_id
    )
    tenant_id = str(tenant["tenant_id"])
    owner_member = str(identity.list_members(tenant_id)[0]["membership_id"])
    catalog = WorkBuddyCatalogRepo(pool)

    def publish(*, adapter_key: str, model_key: str, dimensions: int | None) -> str:
        revision = catalog.publish_model(
            adapter_key=adapter_key,
            model_key=model_key,
            display_name=f"Embedding {model_key}",
            description="Local ONNX embedding model; weights are downloaded by the "
            "personal edition under ~/.octop/embedding_models.",
            embedding_dimensions=dimensions,
            local_model_id=model_key if adapter_key == ADAPTER_KEY_ONNX else None,
            actor_user_id=owner_id,
        )
        return revision.model_revision_id

    local_revision_id = publish(
        adapter_key=ADAPTER_KEY_ONNX, model_key=LOCAL_MODEL_KEY, dimensions=VECTOR_DIMENSIONS
    )
    bge_revision_id = publish(adapter_key="ollama", model_key="bge-m3", dimensions=None)
    narrow_revision_id = publish(
        adapter_key=ADAPTER_KEY_ONNX, model_key=NARROW_LOCAL_MODEL_KEY, dimensions=768
    )
    ungranted_revision_id = publish(
        adapter_key=ADAPTER_KEY_ONNX,
        model_key="intfloat/multilingual-e5-large",
        dimensions=VECTOR_DIMENSIONS,
    )
    for revision_id in (local_revision_id, bge_revision_id, narrow_revision_id):
        catalog.grant_capability(
            tenant_id,
            kind=CAPABILITY_MODEL,
            revision_id=revision_id,
            subject_kind="tenant",
            actor_member_id=owner_member,
        )
    return {
        "tenant_id": tenant_id,
        "owner_user_id": owner_id,
        "local_revision_id": local_revision_id,
        "bge_revision_id": bge_revision_id,
        "narrow_revision_id": narrow_revision_id,
        "ungranted_revision_id": ungranted_revision_id,
    }


def _service(pool: PostgresPool) -> WorkBuddyKnowledgeService:
    return WorkBuddyKnowledgeService(db=pool)


def _actor(world: dict[str, Any]) -> WorkBuddyKnowledgeActor:
    return WorkBuddyKnowledgeActor(
        user_id=world["owner_user_id"], tenant_id=world["tenant_id"], department_id=None
    )


def _insert_revision(
    pool: PostgresPool,
    *,
    actor_user_id: int,
    adapter_key: str,
    embedding_dimensions: int | None,
    local_model_id: str | None,
) -> None:
    """Insert a revision row directly, bypassing the repository's own validation."""
    with workbuddy_transaction(pool, WorkBuddyDbContext.platform()) as conn:
        conn.execute(
            "INSERT INTO workbuddy_platform_model_revisions("
            " adapter_key, model_key, revision, display_name, description, status,"
            " published_by_user_id, published_at, embedding_dimensions, local_model_id"
            ") VALUES (?, ?, 1, 'raw revision', '', 'published', ?, ?, ?, ?)",
            (
                adapter_key,
                f"raw-{uuid.uuid4().hex}",
                actor_user_id,
                now_ts(),
                embedding_dimensions,
                local_model_id,
            ),
        )


def test_a_published_local_revision_reads_back_its_declaration(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """The declaration is the revision's, and the catalog listing carries it."""
    catalog = WorkBuddyCatalogRepo(pool)
    revision = catalog.get_model_revision(world["local_revision_id"])
    assert revision is not None
    assert revision.adapter_key == ADAPTER_KEY_ONNX
    assert revision.model_key == LOCAL_MODEL_KEY
    assert revision.local_model_id == LOCAL_MODEL_KEY
    assert revision.embedding_dimensions == VECTOR_DIMENSIONS
    listed = {
        row.model_revision_id: row
        for row in catalog.list_public_models()
        if row.model_revision_id == world["local_revision_id"]
    }
    assert listed[world["local_revision_id"]].local_model_id == LOCAL_MODEL_KEY


def test_a_hosted_revision_keeps_both_declaration_fields_empty(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """Shipped revisions are unchanged: no width, no local model id."""
    catalog = WorkBuddyCatalogRepo(pool)
    revision = catalog.get_model_revision(world["bge_revision_id"])
    assert revision is not None
    assert revision.model_key == "bge-m3"
    assert revision.embedding_dimensions is None
    assert revision.local_model_id is None


def test_an_authorized_local_revision_can_back_a_base(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """Publish, grant, pin: the local model reaches a knowledge base like any other."""
    base = _service(pool).create_base(
        _actor(world),
        scope="personal",
        name="Local model base",
        model_revision_id=world["local_revision_id"],
    )
    assert base["embedding"]["model_revision_id"] == world["local_revision_id"]
    assert base["embedding"]["adapter_key"] == ADAPTER_KEY_ONNX
    assert base["embedding"]["model_key"] == LOCAL_MODEL_KEY
    assert base["embedding"]["dimensions"] == VECTOR_DIMENSIONS


def test_bge_m3_still_backs_a_base(pool: PostgresPool, world: dict[str, Any]) -> None:
    """The hosted path is untouched by the widening."""
    base = _service(pool).create_base(
        _actor(world),
        scope="personal",
        name="Hosted model base",
        model_revision_id=world["bge_revision_id"],
    )
    assert base["embedding"]["model_key"] == "bge-m3"
    assert base["embedding"]["dimensions"] == VECTOR_DIMENSIONS


def test_a_declared_width_other_than_the_storage_width_is_refused(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """768-dimension local models are publishable but never pinnable: the column is 1024."""
    service = _service(pool)
    before = len(service.list_bases(_actor(world)))
    with pytest.raises(OctopError) as exc:
        service.create_base(
            _actor(world),
            scope="personal",
            name="Too narrow base",
            model_revision_id=world["narrow_revision_id"],
        )
    assert exc.value.code is ErrorCode.MODEL_NOT_CONFIGURED
    assert len(service.list_bases(_actor(world))) == before


def test_an_ungranted_local_revision_is_refused(pool: PostgresPool, world: dict[str, Any]) -> None:
    """A local model is granted explicitly, exactly like a hosted one."""
    with pytest.raises(OctopError) as exc:
        _service(pool).create_base(
            _actor(world),
            scope="personal",
            name="Ungranted base",
            model_revision_id=world["ungranted_revision_id"],
        )
    assert exc.value.code is ErrorCode.WORKBUDDY_CAPABILITY_NOT_APPROVED


def test_the_repository_refuses_an_onnx_revision_without_its_declaration(
    pool: PostgresPool, world: dict[str, Any]
) -> None:
    """An unembeddable local revision never reaches a row through the repository."""
    catalog = WorkBuddyCatalogRepo(pool)
    with pytest.raises(WorkBuddyInvalidInput):
        catalog.publish_model(
            adapter_key=ADAPTER_KEY_ONNX,
            model_key=f"missing-declaration-{uuid.uuid4().hex[:8]}",
            display_name="Missing declaration",
            actor_user_id=world["owner_user_id"],
        )
    with pytest.raises(WorkBuddyInvalidInput):
        catalog.publish_model(
            adapter_key=ADAPTER_KEY_ONNX,
            model_key=f"missing-local-{uuid.uuid4().hex[:8]}",
            display_name="Missing local model",
            embedding_dimensions=VECTOR_DIMENSIONS,
            actor_user_id=world["owner_user_id"],
        )


@pytest.mark.parametrize(
    ("adapter_key", "dimensions", "local_model_id"),
    [
        (ADAPTER_KEY_ONNX, None, None),
        (ADAPTER_KEY_ONNX, VECTOR_DIMENSIONS, None),
        (ADAPTER_KEY_ONNX, None, LOCAL_MODEL_KEY),
        ("ollama", 0, None),
        ("ollama", -768, None),
        ("ollama", VECTOR_DIMENSIONS, "   "),
    ],
)
def test_the_schema_refuses_an_unembeddable_local_revision(
    pool: PostgresPool,
    world: dict[str, Any],
    adapter_key: str,
    dimensions: int | None,
    local_model_id: str | None,
) -> None:
    """A raw write cannot store a shape the embedder could never load."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_revision(
            pool,
            actor_user_id=world["owner_user_id"],
            adapter_key=adapter_key,
            embedding_dimensions=dimensions,
            local_model_id=local_model_id,
        )


def test_revoking_a_local_revision_keeps_the_row(pool: PostgresPool, world: dict[str, Any]) -> None:
    """Audit: revocation flips status and stamps the time; the row is never deleted."""
    catalog = WorkBuddyCatalogRepo(pool)
    actor_id = world["owner_user_id"]
    revision = catalog.publish_model(
        adapter_key=ADAPTER_KEY_ONNX,
        model_key=f"local-{uuid.uuid4().hex[:8]}",
        display_name="Revocation subject",
        embedding_dimensions=VECTOR_DIMENSIONS,
        local_model_id="BAAI/bge-small-en-v1.5",
        actor_user_id=actor_id,
    )
    assert catalog.revoke_model(revision.model_revision_id, actor_user_id=actor_id) is True
    kept = catalog.get_model_revision(revision.model_revision_id)
    assert kept is not None
    assert kept.status == "revoked"
    assert kept.revoked_at is not None
    assert kept.local_model_id == "BAAI/bge-small-en-v1.5"
    assert kept.embedding_dimensions == VECTOR_DIMENSIONS
    assert revision.model_revision_id in {
        row.model_revision_id for row in catalog.list_model_revisions()
    }
    with pool.connect() as conn:
        row = conn.execute(
            "SELECT count(*) AS kept FROM workbuddy_platform_model_revisions"
            " WHERE model_revision_id = ?",
            (revision.model_revision_id,),
        ).fetchone()
    assert int(row["kept"]) == 1
    with pytest.raises(OctopError) as exc:
        _service(pool).create_base(
            _actor(world),
            scope="personal",
            name="Revoked base",
            model_revision_id=revision.model_revision_id,
        )
    assert exc.value.code is ErrorCode.WORKBUDDY_PLATFORM_REVISION_REVOKED

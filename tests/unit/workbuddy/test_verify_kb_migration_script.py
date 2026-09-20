"""The migration verifier: chunk counting and self-retrieval, no writes anywhere.

Verifying an import needs both sides to be real, so the tests build them:

* the source is a small personal control plane plus one ``index.sqlite`` per base
  holding real ``<Nf`` float32 vectors;
* the target is the live PostgreSQL schema (this module resets it, like the other
  knowledge database tests) with bases, documents, generations and chunks created
  through :class:`WorkBuddyKnowledgeRepo`.

What is asserted is what the operator acts on:

* matching chunk counts verify clean and exit ``0``;
* a differing count, and a source document with no target twin, are listed and
  exit non-zero;
* the self-retrieval probe agrees when the vectors were copied, and reports the
  neighbours the target does not share when they were not;
* a probe whose k-th neighbour is tied is stood down instead of reported, since
  both sides may fill that slot with either chunk;
* the report has the documented JSON shape and the source files are untouched;
* an unreadable source is an error in the report, never a traceback.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import sqlite3
import struct
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "verify_kb_migration.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("verify_kb_migration_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


verify_kb = _load_script()

CONTROL_PLANE_SCHEMA = (
    "CREATE TABLE knowledge_bases ("
    "  id INTEGER PRIMARY KEY, knowledge_base_id TEXT NOT NULL UNIQUE,"
    "  owner_user_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',"
    "  default_open INTEGER NOT NULL DEFAULT 0, shared INTEGER NOT NULL DEFAULT 0,"
    "  icon_name TEXT NOT NULL DEFAULT '', embedding_model TEXT NOT NULL DEFAULT '',"
    "  embedding_dim INTEGER NOT NULL DEFAULT 0, doc_count INTEGER NOT NULL DEFAULT 0,"
    "  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
)
DOCUMENTS_SCHEMA = (
    "CREATE TABLE knowledge_documents ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL UNIQUE,"
    "  kb_id TEXT NOT NULL, path TEXT NOT NULL, filename TEXT NOT NULL,"
    "  is_dir INTEGER NOT NULL DEFAULT 0, content_type TEXT NOT NULL DEFAULT 'text/markdown',"
    "  byte_size INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '',"
    "  status TEXT NOT NULL DEFAULT 'ready', error_message TEXT NOT NULL DEFAULT '',"
    "  chunk_count INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,"
    "  updated_at INTEGER NOT NULL, UNIQUE(kb_id, path))"
)
INDEX_SCHEMA = (
    "CREATE TABLE chunks ("
    "  chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ordinal INTEGER NOT NULL,"
    "  text TEXT NOT NULL, embedding BLOB NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}')"
)

MODEL_REVISION_ID = "cccccccc-0000-4000-8000-0000000000f5"
MODEL = WorkBuddyPlatformModelRevisionRow(
    model_revision_id=MODEL_REVISION_ID,
    adapter_key="ollama",
    model_key="bge-m3",
    revision=1,
    display_name="bge-m3 r1",
    status="published",
)

#: Every document is given its own angular neighbourhood so that no two chunks
#: in a base score the same against a probe: ties would make the comparison
#: depend on how the other side breaks them.
DOCUMENT_OFFSET = 1.2


def _vector(angle: float) -> list[float]:
    values = [0.0] * 1024
    values[0] = math.cos(angle)
    values[1] = math.sin(angle)
    return values


def _chunk_vector(document_index: int, ordinal: int) -> list[float]:
    return _vector(document_index * DOCUMENT_OFFSET + ordinal * 0.1)


def _document_vectors(
    document_index: int,
    texts: list[str],
    angles: dict[str, list[float]] | None,
    title: str,
) -> list[list[float]]:
    """One vector per chunk, from the explicit angles for that title when given."""
    given = (angles or {}).get(title)
    if given is not None:
        assert len(given) == len(texts), f"{title}: {len(given)} angles for {len(texts)} chunks"
        return [_vector(angle) for angle in given]
    return [_chunk_vector(document_index, ordinal) for ordinal in range(len(texts))]


def _unique(name: str = "Handbook") -> str:
    """A base name no other test in this run shares (the match is by name)."""
    return f"{name}-{uuid.uuid4().hex[:8]}"


def _blob(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _write_source(
    root: Path,
    *,
    name: str = "Handbook",
    owner_user_id: int = 7,
    kb_id: str = "kb-personal",
    documents: list[tuple[str, str, list[str]]] | None = None,
    angles: dict[str, list[float]] | None = None,
    corrupt: bool = False,
) -> tuple[Path, Path]:
    """A personal control plane plus one ``index.sqlite``, holding real vectors.

    ``angles`` overrides the default vector family per document title — a document
    given two chunks at the same angle is how a tied k-th neighbour is built.
    """
    documents = documents or []
    knowledge_dir = root / "knowledge"
    (knowledge_dir / kb_id).mkdir(parents=True, exist_ok=True)
    db_path = root / "octop.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(CONTROL_PLANE_SCHEMA)
        connection.execute(DOCUMENTS_SCHEMA)
        connection.execute(
            "INSERT INTO knowledge_bases (knowledge_base_id, owner_user_id, name, "
            "default_open, shared, embedding_model, embedding_dim, doc_count, "
            "created_at, updated_at) VALUES (?, ?, ?, 0, 0, 'bge-m3', 1024, ?, 1, 1)",
            (kb_id, owner_user_id, name, len(documents)),
        )
        for document_id, title, texts in documents:
            connection.execute(
                "INSERT INTO knowledge_documents (document_id, kb_id, path, filename, "
                "chunk_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1)",
                (document_id, kb_id, title, title, len(texts)),
            )
    index_path = knowledge_dir / kb_id / "index.sqlite"
    if corrupt:
        index_path.write_bytes(b"this is not a database")
        return knowledge_dir, db_path
    with sqlite3.connect(index_path) as connection:
        connection.execute(INDEX_SCHEMA)
        for document_index, (document_id, title, texts) in enumerate(documents):
            vectors = _document_vectors(document_index, texts, angles, title)
            for ordinal, text in enumerate(texts):
                connection.execute(
                    "INSERT INTO chunks (chunk_id, doc_id, ordinal, text, embedding, meta_json) "
                    "VALUES (?, ?, ?, ?, ?, '{}')",
                    (
                        f"{document_id}:{ordinal}",
                        document_id,
                        ordinal,
                        text,
                        _blob(vectors[ordinal]),
                    ),
                )
    return knowledge_dir, db_path


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify(
    knowledge_dir: Path,
    db_path: Path,
    *,
    target_db: str | None = None,
    tenant_id: str | None = None,
    sample: int = 1,
    top_k: int = 2,
) -> dict[str, Any]:
    return verify_kb.verify(
        source_dir=knowledge_dir,
        source_db=db_path,
        target_db=target_db,
        tenant_id=tenant_id,
        sample=sample,
        top_k=top_k,
    )


# ── the target side: a tenant, a base and published chunks ───────────────────


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
    """One tenant with a member to own the migrated bases."""
    identity = WorkBuddyIdentityRepo(pool)
    with pool.connect() as conn:
        user_id = int(
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, 'hash', 'user', ?) RETURNING id",
                (f"kb-verify-{uuid.uuid4().hex[:8]}", now_ts()),
            ).fetchone()["id"]
        )
    tenant = identity.create_tenant(
        f"kb-verify-{uuid.uuid4().hex[:8]}", "Verification tenant", owner_user_id=user_id
    )
    tenant_id = str(tenant["tenant_id"])
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_platform_model_revisions "
            "(model_revision_id, adapter_key, model_key, revision, display_name, status, "
            "published_by_user_id, published_at) "
            "VALUES (?, 'ollama', 'bge-m3', 1, 'Verification model', 'published', ?, ?)",
            (MODEL_REVISION_ID, user_id, now_ts()),
        )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "ctx": WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id),
    }


def _seed_target_base(
    pool: PostgresPool,
    world: dict[str, Any],
    *,
    name: str,
    documents: list[tuple[str, list[str]]],
    angles: dict[str, list[float]] | None = None,
    shorten: str | None = None,
    reverse_vectors: bool = False,
) -> str:
    """Publish one ``migration`` document per entry; returns the base id.

    ``shorten`` drops the last chunk of the document with that title, which is
    how a count mismatch is produced; ``reverse_vectors`` re-orders each
    document's vectors so the same text is no longer the nearest neighbour.
    """
    repo = WorkBuddyKnowledgeRepo(pool)
    base = repo.create_base(
        world["ctx"],
        scope="personal",
        name=name,
        description="",
        model=MODEL,
        created_by_user_id=world["user_id"],
        owner_user_id=world["user_id"],
    )
    for document_index, (title, texts) in enumerate(documents):
        contents = texts[:-1] if title == shorten else texts
        vectors = _document_vectors(document_index, texts, angles, title)
        if reverse_vectors:
            vectors = list(reversed(vectors))
        document = repo.create_document(
            world["ctx"],
            base.kb_id,
            file_ref_id=None,
            source="migration",
            title=title,
            created_by_user_id=world["user_id"],
        )
        chunks = [
            (ordinal, text, 0, {}, vectors[ordinal]) for ordinal, text in enumerate(contents)
        ]
        repo.publish_generation(
            world["ctx"],
            base=base,
            document_id=document.document_id,
            created_by_user_id=world["user_id"],
            chunks=chunks,
        )
    return base.kb_id


DOCUMENT_TEXTS = [
    "Migration handbook introduction: what the personal edition stored.",
    "Migration handbook details: chunks keep their text and their vectors.",
    "Migration handbook notes: the target is the only retrievable copy.",
    "Migration handbook appendix: nothing here needs a model provider.",
]


@requires_postgresql
@pytest.mark.postgresql
def test_matching_counts_verify_clean_and_exit_zero(
    pool: PostgresPool, world: dict[str, Any], tmp_path: Path
) -> None:
    """The import landed: documents and their chunk counts line up, exit code 0."""
    name = _unique()
    knowledge_dir, db_path = _write_source(
        tmp_path,
        name=name,
        owner_user_id=world["user_id"],
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS)],
    )
    kb_id = _seed_target_base(
        pool,
        world,
        name=name,
        documents=[("Handbook.md", DOCUMENT_TEXTS)],
    )
    report = _verify(
        knowledge_dir, db_path, target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
        tenant_id=world["tenant_id"],
    )
    entry = report["knowledge_bases"][0]
    assert (report["differences"], report["errors"]) == ([], [])
    assert entry["target_kb_id"] == kb_id
    assert entry["target_match"] == "creator+name"
    assert entry["documents"]["matched"] == 1
    assert entry["documents"]["mismatched"] == []
    assert entry["chunks"] == {"source": 4, "target": 4, "matched": 4}
    assert entry["ok"] is True
    exit_code = verify_kb.main(
        [
            "--source-dir",
            str(knowledge_dir),
            "--source-db",
            str(db_path),
            "--target-db",
            os.environ["OCTOP_TEST_DATABASE_URL"],
            "--tenant-id",
            world["tenant_id"],
            "--sample",
            "1",
            "--top-k",
            "2",
            "--quiet",
        ]
    )
    assert exit_code == 0


@requires_postgresql
@pytest.mark.postgresql
def test_count_differences_and_missing_documents_are_listed(
    pool: PostgresPool, world: dict[str, Any], tmp_path: Path
) -> None:
    """A target count that differs, and a source document with no target row, fail."""
    name = _unique()
    knowledge_dir, db_path = _write_source(
        tmp_path,
        name=name,
        owner_user_id=world["user_id"],
        documents=[
            ("doc-a", "Handbook.md", DOCUMENT_TEXTS),
            ("doc-b", "Removed.md", ["Removed chapter one.", "Removed chapter two."]),
        ],
    )
    _seed_target_base(
        pool,
        world,
        name=name,
        documents=[("Handbook.md", DOCUMENT_TEXTS)],
        shorten="Handbook.md",
    )
    report = _verify(
        knowledge_dir, db_path, target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
        tenant_id=world["tenant_id"],
    )
    documents = report["knowledge_bases"][0]["documents"]
    assert documents["source"] == 2
    assert documents["matched"] == 1
    mismatched = documents["mismatched"]
    assert len(mismatched) == 1
    assert mismatched[0]["doc_id"] == "doc-a"
    assert mismatched[0]["title"] == "Handbook.md"
    assert mismatched[0]["target_document_id"]
    assert (mismatched[0]["source_chunks"], mismatched[0]["target_chunks"]) == (4, 3)
    assert documents["missing"] == [
        {"doc_id": "doc-b", "title": "Removed.md", "chunks": 2}
    ]
    kinds = {difference["kind"] for difference in report["differences"]}
    assert kinds == {"document_chunks", "document_missing"}
    count_difference = next(
        difference
        for difference in report["differences"]
        if difference["kind"] == "document_chunks"
    )
    assert "source 4 chunks" in count_difference["detail"]
    assert "target 3" in count_difference["detail"]
    missing = next(
        difference
        for difference in report["differences"]
        if difference["kind"] == "document_missing"
    )
    assert "'Removed.md'" in missing["detail"] and "2 source chunks" in missing["detail"]
    exit_code = verify_kb.main(
        [
            "--source-dir",
            str(knowledge_dir),
            "--source-db",
            str(db_path),
            "--target-db",
            os.environ["OCTOP_TEST_DATABASE_URL"],
            "--tenant-id",
            world["tenant_id"],
            "--sample",
            "1",
            "--top-k",
            "2",
            "--quiet",
        ]
    )
    assert exit_code == 1


#: ``Handbook.md`` deliberately holds two chunks at the same angle.  Every probe
#: near them has a k-th neighbour tied with the next one, which each side may fill
#: arbitrarily — the verifier must stand that comparison down rather than report a
#: difference the migration did not cause.  ``Policy.md`` stays tie-free, so a
#: probe that really was compared is still present.
TIED_ANGLES = {"Handbook.md": [0.0, 0.1, 0.1, 0.2]}
POLICY_TEXTS = ["Policy alpha.", "Policy beta.", "Policy gamma."]


@requires_postgresql
@pytest.mark.postgresql
def test_self_retrieval_agrees_when_the_vectors_were_copied(
    pool: PostgresPool, world: dict[str, Any], tmp_path: Path
) -> None:
    """Copied vectors agree, and a probe whose neighbour slot is tied is stood down."""
    name = _unique()
    knowledge_dir, db_path = _write_source(
        tmp_path,
        name=name,
        owner_user_id=world["user_id"],
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS), ("doc-b", "Policy.md", POLICY_TEXTS)],
        angles=TIED_ANGLES,
    )
    _seed_target_base(
        pool,
        world,
        name=name,
        documents=[("Handbook.md", DOCUMENT_TEXTS), ("Policy.md", POLICY_TEXTS)],
        angles=TIED_ANGLES,
    )
    report = _verify(
        knowledge_dir, db_path, target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
        tenant_id=world["tenant_id"], sample=3, top_k=2,
    )
    entry = report["knowledge_bases"][0]
    retrieval = entry["retrieval"]
    assert (report["differences"], report["errors"]) == ([], [])
    assert retrieval["tied"] >= 1, "the twin chunks must be recognised as a tie"
    assert retrieval["compared"] >= 1, "the tie-free probes must still be compared"
    assert retrieval["compared"] + retrieval["tied"] == retrieval["sampled"]
    assert retrieval["hit_rate"] == 1.0
    assert all(probe["shared"] == probe["of"] == 2 for probe in retrieval["probes"])
    assert any("tied" in note for note in entry["notes"])
    assert report["totals"]["retrieval"]["hit_rate"] == 1.0
    assert report["totals"]["retrieval"]["tied"] >= 1


@requires_postgresql
@pytest.mark.postgresql
def test_self_retrieval_reports_neighbours_the_target_does_not_share(
    pool: PostgresPool, world: dict[str, Any], tmp_path: Path
) -> None:
    """A target that stored different vectors cannot answer the source's probe."""
    name = _unique()
    knowledge_dir, db_path = _write_source(
        tmp_path,
        name=name,
        owner_user_id=world["user_id"],
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS)],
    )
    _seed_target_base(
        pool,
        world,
        name=name,
        documents=[("Handbook.md", DOCUMENT_TEXTS)],
        reverse_vectors=True,
    )
    report = _verify(
        knowledge_dir, db_path, target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
        tenant_id=world["tenant_id"],
    )
    retrieval = report["knowledge_bases"][0]["retrieval"]
    probe = retrieval["probes"][0]
    prefix = verify_kb.PREFIX_LENGTH
    assert retrieval["hit_rate"] == 0.0
    assert probe["shared"] == 0
    assert probe["missing"] == sorted(
        [DOCUMENT_TEXTS[0][:prefix], DOCUMENT_TEXTS[1][:prefix]]
    )
    assert probe["unexpected"] == sorted(
        [DOCUMENT_TEXTS[2][:prefix], DOCUMENT_TEXTS[3][:prefix]]
    )
    assert any(
        difference["kind"] == "retrieval" for difference in report["differences"]
    )


@requires_postgresql
@pytest.mark.postgresql
def test_the_report_has_the_documented_shape_and_the_source_is_untouched(
    pool: PostgresPool, world: dict[str, Any], tmp_path: Path
) -> None:
    """``--report`` writes the same JSON the summary is built from, read-only."""
    name = _unique()
    knowledge_dir, db_path = _write_source(
        tmp_path,
        name=name,
        owner_user_id=world["user_id"],
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS)],
    )
    _seed_target_base(pool, world, name=name, documents=[("Handbook.md", DOCUMENT_TEXTS)])
    index_path = knowledge_dir / "kb-personal" / "index.sqlite"
    before = (_fingerprint(db_path), _fingerprint(index_path))
    report_path = tmp_path / "verification.json"
    exit_code = verify_kb.main(
        [
            "--source-dir",
            str(knowledge_dir),
            "--source-db",
            str(db_path),
            "--target-db",
            os.environ["OCTOP_TEST_DATABASE_URL"],
            "--tenant-id",
            world["tenant_id"],
            "--report",
            str(report_path),
            "--quiet",
        ]
    )
    assert exit_code == 0
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert set(written) == {
        "source_dir",
        "source_db",
        "target",
        "options",
        "knowledge_bases",
        "totals",
        "differences",
        "errors",
    }
    assert written["target"]["checked"] is True
    assert written["options"] == {"sample": 5, "top_k": 5, "prefix_length": 48}
    assert set(written["knowledge_bases"][0]) == {
        "kb_id",
        "name",
        "owner_user_id",
        "target_scope",
        "target_kb_id",
        "target_match",
        "documents",
        "chunks",
        "retrieval",
        "notes",
        "ok",
    }
    assert set(written["totals"]) == {
        "knowledge_bases",
        "verified",
        "documents",
        "chunks",
        "retrieval",
    }
    assert written["totals"]["documents"] == {
        "source": 1,
        "matched": 1,
        "missing": 0,
        "mismatched": 0,
        "extra": 0,
    }
    assert written["totals"]["chunks"] == {"source": 4, "target": 4, "matched": 4}
    assert written["totals"]["retrieval"]["hit_rate"] == 1.0
    assert (_fingerprint(db_path), _fingerprint(index_path)) == before


def test_an_unreadable_source_is_an_error_in_the_report(tmp_path: Path) -> None:
    """A missing control plane or a corrupt index is reported, never a traceback."""
    knowledge_dir, db_path = _write_source(
        tmp_path,
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS)],
    )
    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt_dir, corrupt_db = _write_source(
        corrupt_root,
        kb_id="kb-corrupt",
        name="Corrupt",
        documents=[("doc-a", "Handbook.md", DOCUMENT_TEXTS)],
        corrupt=True,
    )
    # No target at all: the report must still be produced, and say why it cannot verify.
    report = _verify(knowledge_dir, db_path)
    assert report["knowledge_bases"][0]["target_match"] == "unchecked"
    assert any("no target database" in error for error in report["errors"])

    corrupt_report = _verify(corrupt_dir, corrupt_db)
    assert any(
        "unreadable index" in error for error in corrupt_report["errors"]
    ), corrupt_report["errors"]

    missing = verify_kb.verify(
        source_dir=knowledge_dir,
        source_db=tmp_path / "absent.db",
        target_db=None,
        tenant_id=None,
    )
    assert any("source control plane" in error for error in missing["errors"])
    assert missing["knowledge_bases"] == []

"""The knowledge-base migration reconciler: counts, import paths, no side effects.

The script reads two sources the platform cannot invent — a personal
control-plane SQLite database and one ``index.sqlite`` per base — so the tests
build them: a small control-plane table and chunk tables holding real ``<Nf``
float32 embeddings.  What is asserted here is what the operator will act on:

* the counts are per base (documents, chunks) and the vector dimension comes from
  the stored bytes, not from metadata;
* the import path follows the plan: copy, re-embed, suspend, or unknown when no
  target database was handed in;
* re-running is safe: the source is never written, and the analysis is identical;
* a broken source (missing database, corrupt index) is reported, not a traceback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import struct
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from tests.support.postgresql import requires_postgresql

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "migrate_kb.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("migrate_kb_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


migrate_kb = _load_script()

PERSONAL_SCHEMA = (
    "CREATE TABLE knowledge_bases ("
    "  id INTEGER PRIMARY KEY, knowledge_base_id TEXT NOT NULL UNIQUE,"
    "  owner_user_id INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',"
    "  default_open INTEGER NOT NULL DEFAULT 0, shared INTEGER NOT NULL DEFAULT 0,"
    "  icon_name TEXT NOT NULL DEFAULT '', embedding_model TEXT NOT NULL DEFAULT '',"
    "  embedding_dim INTEGER NOT NULL DEFAULT 0, doc_count INTEGER NOT NULL DEFAULT 0,"
    "  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
)
INDEX_SCHEMA = (
    "CREATE TABLE chunks ("
    "  chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, ordinal INTEGER NOT NULL,"
    "  text TEXT NOT NULL, embedding BLOB NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}')"
)


def _embedding(dimensions: int) -> bytes:
    return struct.pack(f"<{dimensions}f", *([0.5] * dimensions))


def _write_source(
    root: Path,
    *,
    bases: list[dict[str, Any]],
    index_chunks: dict[str, list[tuple[str, int, int]]] | None = None,
    corrupt: set[str] | None = None,
) -> tuple[Path, Path]:
    """A personal control plane plus one ``index.sqlite`` per base."""
    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    db_path = root / "octop.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(PERSONAL_SCHEMA)
        for order, base in enumerate(bases):
            connection.execute(
                "INSERT INTO knowledge_bases (knowledge_base_id, owner_user_id, name, "
                "description, default_open, shared, embedding_model, embedding_dim, doc_count, "
                "created_at, updated_at) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
                (
                    base["kb_id"],
                    base.get("owner_user_id", 7),
                    base["name"],
                    int(base.get("default_open", 0)),
                    int(base.get("shared", 0)),
                    base.get("embedding_model", "bge-m3"),
                    base.get("embedding_dim", 1024),
                    base.get("doc_count", 0),
                    1_700_000_000 + order,
                    1_700_000_000 + order,
                ),
            )
    for kb_id, chunks in (index_chunks or {}).items():
        kb_dir = knowledge_dir / kb_id
        kb_dir.mkdir(parents=True, exist_ok=True)
        index_path = kb_dir / "index.sqlite"
        if corrupt and kb_id in corrupt:
            index_path.write_bytes(b"this is not a database")
            continue
        with sqlite3.connect(index_path) as connection:
            connection.execute(INDEX_SCHEMA)
            for chunk_id, doc_id, dimensions in chunks:
                connection.execute(
                    "INSERT INTO chunks (chunk_id, doc_id, ordinal, text, embedding, meta_json) "
                    "VALUES (?, ?, 0, 'chunk text', ?, '{}')",
                    (chunk_id, doc_id, _embedding(dimensions)),
                )
    return knowledge_dir, db_path


def _analyse(
    root: Path,
    *,
    bases: list[dict[str, Any]],
    index_chunks: dict[str, list[tuple[str, int, int]]] | None = None,
    corrupt: set[str] | None = None,
    target_db: str | None = None,
    tenant_id: str | None = None,
    model_revision_id: str | None = None,
) -> dict[str, Any]:
    knowledge_dir, db_path = _write_source(
        root, bases=bases, index_chunks=index_chunks, corrupt=corrupt
    )
    return migrate_kb.analyse(
        source_dir=knowledge_dir,
        source_db=db_path,
        target_db=target_db,
        tenant_id=tenant_id,
        model_revision_id=model_revision_id,
    )


def test_counts_and_dimension_come_from_the_source(tmp_path: Path) -> None:
    report = _analyse(
        tmp_path,
        bases=[
            {"kb_id": "kb-1", "name": "Handbook", "shared": 1, "embedding_dim": 1024},
            {"kb_id": "kb-2", "name": "Notes"},
        ],
        index_chunks={
            "kb-1": [("c1", "doc-a", 1024), ("c2", "doc-a", 1024), ("c3", "doc-b", 1024)],
            "kb-2": [("c1", "doc-c", 768)],
        },
    )

    by_id = {entry["kb_id"]: entry for entry in report["knowledge_bases"]}
    assert by_id["kb-1"]["documents"] == 2
    assert by_id["kb-1"]["chunks"] == 3
    assert by_id["kb-1"]["vector_dim"] == 1024
    assert by_id["kb-1"]["target_scope"] == "enterprise"  # shared = 1 in the personal edition
    assert by_id["kb-2"]["target_scope"] == "personal"
    assert by_id["kb-2"]["vector_dim"] == 768
    assert report["totals"] == {
        "knowledge_bases": 2,
        "documents": 3,
        "chunks": 4,
        "by_path": {"A": 0, "B": 0, "C": 0, "unknown": 2},
    }
    # The import policy of this batch is stated, not implied.
    assert report["import_policy"]["document_source"] == "migration"
    assert "not in this batch" in report["import_policy"]["apply"]


def test_a_base_without_chunks_is_suspended(tmp_path: Path) -> None:
    report = _analyse(
        tmp_path,
        bases=[{"kb_id": "kb-empty", "name": "Empty"}],
        index_chunks={"kb-empty": []},
    )
    entry = report["knowledge_bases"][0]
    assert entry["path"] == "C"
    assert "no chunks" in entry["reason"]


def test_a_missing_index_is_reported_not_crashed(tmp_path: Path) -> None:
    report = _analyse(tmp_path, bases=[{"kb_id": "kb-gone", "name": "Gone"}], index_chunks={})
    entry = report["knowledge_bases"][0]
    assert entry["path"] == "C"
    assert "index.sqlite is missing" in entry["reason"]
    assert report["errors"] == []


def test_a_corrupt_index_only_affects_its_own_base(tmp_path: Path) -> None:
    report = _analyse(
        tmp_path,
        bases=[{"kb_id": "kb-bad", "name": "Bad"}, {"kb_id": "kb-good", "name": "Good"}],
        index_chunks={"kb-bad": [("c1", "doc", 1024)], "kb-good": [("c1", "doc", 1024)]},
        corrupt={"kb-bad"},
    )
    by_id = {entry["kb_id"]: entry for entry in report["knowledge_bases"]}
    assert by_id["kb-bad"]["path"] == "C"
    assert "unreadable index" in by_id["kb-bad"]["reason"]
    assert by_id["kb-good"]["path"] == "unknown"  # no target given, so unchecked
    assert by_id["kb-good"]["documents"] == 1


def test_running_twice_changes_nothing(tmp_path: Path) -> None:
    """Idempotent by construction: the source is read-only and the report is stable."""
    knowledge_dir, db_path = _write_source(
        tmp_path,
        bases=[{"kb_id": "kb-1", "name": "Handbook", "shared": 1}],
        index_chunks={"kb-1": [("c1", "doc-a", 1024), ("c2", "doc-b", 1024)]},
    )
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (db_path, knowledge_dir / "kb-1" / "index.sqlite")
    }

    first = migrate_kb.analyse(
        source_dir=knowledge_dir,
        source_db=db_path,
        target_db=None,
        tenant_id=None,
        model_revision_id=None,
    )
    second = migrate_kb.analyse(
        source_dir=knowledge_dir,
        source_db=db_path,
        target_db=None,
        tenant_id=None,
        model_revision_id=None,
    )

    assert first == second
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in (db_path, knowledge_dir / "kb-1" / "index.sqlite")
    }
    assert after == before, "the reconciler must never write to the source"


def test_a_missing_control_plane_is_an_error_not_a_crash(tmp_path: Path) -> None:
    report = migrate_kb.analyse(
        source_dir=tmp_path / "knowledge",
        source_db=tmp_path / "absent.db",
        target_db=None,
        tenant_id=None,
        model_revision_id=None,
    )
    assert report["knowledge_bases"] == []
    assert report["errors"] and "not found" in report["errors"][0]

    exit_code = migrate_kb.main(
        ["--source-db", str(tmp_path / "absent.db"), "--source-dir", str(tmp_path), "--quiet"]
    )
    assert exit_code == 1


def test_embeddings_are_decoded_as_little_endian_float32() -> None:
    blob = _embedding(3)
    assert migrate_kb.decode_embedding(blob) == [0.5, 0.5, 0.5]
    with pytest.raises(ValueError):
        migrate_kb.decode_embedding(b"not four-byte aligned")


def test_the_cli_reports_json_and_writes_the_report(tmp_path: Path) -> None:
    knowledge_dir, db_path = _write_source(
        tmp_path,
        bases=[{"kb_id": "kb-1", "name": "Handbook"}],
        index_chunks={"kb-1": [("c1", "doc-a", 1024)]},
    )
    report_path = tmp_path / "report.json"
    exit_code = migrate_kb.main(
        [
            "--source-db",
            str(db_path),
            "--source-dir",
            str(knowledge_dir),
            "--report",
            str(report_path),
            "--quiet",
        ]
    )
    assert exit_code == 0
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["knowledge_bases"][0]["kb_id"] == "kb-1"
    assert written["totals"]["chunks"] == 1


# ── the target side: copy, re-embed or suspend ───────────────────────────────


@requires_postgresql
def test_the_import_path_follows_the_published_model_revision(tmp_path: Path) -> None:
    """A granted 1024-dimension model copies; a smaller one re-embeds; an ungranted one waits."""
    pool = _target_pool()
    try:
        tenant_id = _seed_tenant(pool, tmp_path)
        granted_revision = _seed_revision(pool, granted=True, tenant_id=tenant_id)
        ungranted_revision = _seed_revision(pool, granted=False, tenant_id=tenant_id)

        knowledge_dir, db_path = _write_source(
            tmp_path,
            bases=[
                {"kb_id": "kb-copy", "name": "Copy"},
                {"kb_id": "kb-reembed", "name": "Re-embed", "embedding_dim": 1024},
            ],
            index_chunks={
                "kb-copy": [("c1", "doc-a", 1024)],
                "kb-reembed": [("c1", "doc-a", 768)],
            },
        )

        matched = migrate_kb.analyse(
            source_dir=knowledge_dir,
            source_db=db_path,
            target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
            tenant_id=tenant_id,
            model_revision_id=granted_revision,
        )
        by_id = {entry["kb_id"]: entry for entry in matched["knowledge_bases"]}
        assert by_id["kb-copy"]["path"] == "A"
        assert by_id["kb-reembed"]["path"] == "B"
        # The metadata says 1024 while the vectors hold 768: kept as a caveat, and
        # the path still follows the vectors, which are the thing being copied.
        assert any("metadata says" in note for note in by_id["kb-reembed"]["notes"])

        suspended = migrate_kb.analyse(
            source_dir=knowledge_dir,
            source_db=db_path,
            target_db=os.environ["OCTOP_TEST_DATABASE_URL"],
            tenant_id=tenant_id,
            model_revision_id=ungranted_revision,
        )
        assert {entry["path"] for entry in suspended["knowledge_bases"]} == {"C"}
        assert all("not been granted" in entry["reason"] for entry in suspended["knowledge_bases"])
    finally:
        pool.close()


def _target_pool() -> Any:
    from octop.infra.db.pool import PostgresPool

    return PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"], min_size=1, max_size=1)


def _seed_tenant(pool: Any, tmp_path: Path) -> str:
    """One tenant with an owner, created on the live database."""
    from octop.infra.db.repos._base import now_ts
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    with pool.connect() as conn:
        user_id = int(
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, 'hash', 'user', ?) RETURNING id",
                (f"kb-migrate-{uuid.uuid4().hex[:8]}", now_ts()),
            ).fetchone()["id"]
        )
    tenant = WorkBuddyIdentityRepo(pool).create_tenant(
        f"kb-migrate-{uuid.uuid4().hex[:8]}", "Migration tenant", owner_user_id=user_id
    )
    return str(tenant["tenant_id"])


def _seed_revision(pool: Any, *, granted: bool, tenant_id: str) -> str:
    """A published model revision, optionally granted to the tenant."""
    from octop.infra.db.repos._base import now_ts

    revision_id = str(uuid.uuid4())
    # (adapter_key, model_key, revision) is unique, so each seed takes its own number.
    revision_number = 1 + int(uuid.uuid4().hex[:4], 16) % 90
    with pool.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO workbuddy_platform_model_revisions "
            "(model_revision_id, adapter_key, model_key, revision, display_name, status, "
            "published_by_user_id, published_at) "
            "VALUES (?, 'ollama', 'bge-m3', ?, 'Migration model', 'published', 1, ?)",
            (revision_id, revision_number, now_ts()),
        )
        if granted:
            conn.execute(
                "INSERT INTO workbuddy_tenant_model_grants (tenant_id, model_revision_id, "
                "granted_at) VALUES (?, ?, ?)",
                (tenant_id, revision_id, now_ts()),
            )
    return revision_id

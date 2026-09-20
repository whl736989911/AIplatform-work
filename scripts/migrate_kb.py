#!/usr/bin/env python3
"""Reconcile the personal edition's knowledge bases before importing them (B-08).

The enterprise platform's knowledge bases are PostgreSQL-only and pin one
platform model revision per base, while the personal edition stored each base as
a SQLite file: ``~/.octop/knowledge/<kb_id>/index.sqlite`` with a ``chunks`` table
holding the chunk text and a ``<N>f`` little-endian float32 embedding, plus a row
in the personal control plane's ``knowledge_bases`` table describing the base
(``shared``, ``default_open``, ``embedding_model``, ``embedding_dim``).

This script is the **analysis** half of that migration: it reads both sides and
reports, per base, what the import will do and why.

    A  copy the vectors as they are — the target's pinned model revision is
       published, is granted to the tenant, and the stored vectors already have
       the enterprise dimension;
    B  re-embed — the model is usable but the vectors do not match its
       dimension, so they must be regenerated;
    C  suspend — the model revision is missing or not granted to the tenant, so an
       administrator has to authorize it before anything can be imported;
    unknown — no target database was handed in, so nothing could be checked.

Nothing here writes to the source, and this batch does not write to the target
either: the report is the deliverable (``--report`` writes it as JSON, and the
same summary goes to stdout).  Idempotent by construction — re-running only
re-reads, and the analysis is a pure function of the two sources.

Usage:

    python scripts/migrate_kb.py                       # analyse ~/.octop against $OCTOP_DATABASE_URL
    python scripts/migrate_kb.py --source-dir /backup/knowledge --source-db /backup/octop.db \
        --target-db postgresql://user:pass@host/db --tenant-id <uuid> --model-revision-id <uuid> \
        --report migration-report.json

The import itself (creating bases, publishing generations, filling the ACL and
preferences) lands with B-10/B-12/B-15; this script states that in its report as
``"apply": "not in this batch"`` so nobody mistakes a clean report for an import.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The enterprise knowledge tables fix the embedding dimension at 1024.
ENTERPRISE_DIMENSIONS = 1024

#: Import paths, as published by the plan (docs/plan/two-machine-workstreams.md).
PATH_COPY = "A"
PATH_REEMBED = "B"
PATH_SUSPEND = "C"
PATH_UNKNOWN = "unknown"

#: How a migrated document is represented in the enterprise schema: text only.
IMPORT_DOCUMENT_SOURCE = "migration"


@dataclass(slots=True)
class SourceBase:
    """One row of the personal control plane's ``knowledge_bases``."""

    kb_id: str
    name: str
    owner_user_id: int
    shared: bool
    default_open: bool
    embedding_model: str
    embedding_dim: int
    doc_count: int

    @property
    def target_scope(self) -> str:
        """``shared`` meant "visible to everyone" in the personal edition."""
        return "enterprise" if self.shared else "personal"


@dataclass(slots=True)
class SourceIndex:
    """What one ``index.sqlite`` holds."""

    path: Path
    documents: int = 0
    chunks: int = 0
    dimensions: set[int] = field(default_factory=set)
    error: str = ""

    @property
    def dimension(self) -> int | None:
        """The vector dimension seen in the file, when it is consistent."""
        if len(self.dimensions) == 1:
            return next(iter(self.dimensions))
        return None


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a source database read-only; the migration never writes to it."""
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def read_source_bases(db_path: Path) -> list[SourceBase]:
    """Every base in the personal control plane, oldest first."""
    if not db_path.exists():
        raise FileNotFoundError(f"personal control-plane database not found: {db_path}")
    with _connect_readonly(db_path) as connection:
        rows = connection.execute(
            "SELECT knowledge_base_id, name, owner_user_id, shared, default_open, "
            "embedding_model, embedding_dim, doc_count FROM knowledge_bases "
            "ORDER BY created_at, knowledge_base_id"
        ).fetchall()
    return [
        SourceBase(
            kb_id=str(row["knowledge_base_id"]),
            name=str(row["name"]),
            owner_user_id=int(row["owner_user_id"]),
            shared=bool(row["shared"]),
            default_open=bool(row["default_open"]),
            embedding_model=str(row["embedding_model"] or ""),
            embedding_dim=int(row["embedding_dim"] or 0),
            doc_count=int(row["doc_count"] or 0),
        )
        for row in rows
    ]


def read_source_index(index_path: Path) -> SourceIndex:
    """Count a base's documents and chunks and read the stored vector dimension."""
    index = SourceIndex(path=index_path)
    if not index_path.exists():
        index.error = "index.sqlite is missing"
        return index
    try:
        with _connect_readonly(index_path) as connection:
            totals = connection.execute(
                "SELECT count(*) AS chunks, count(DISTINCT doc_id) AS documents FROM chunks"
            ).fetchone()
            index.chunks = int(totals["chunks"])
            index.documents = int(totals["documents"])
            for row in connection.execute("SELECT length(embedding) AS size FROM chunks"):
                size = int(row["size"] or 0)
                if size and size % 4 == 0:
                    index.dimensions.add(size // 4)
    except sqlite3.Error as exc:  # a truncated or foreign file must not abort the run
        index.error = f"unreadable index: {exc}"
    # A vector that is not a whole number of float32 values is a data defect, not
    # a dimension: report it instead of guessing one.
    if not index.error and index.chunks and not index.dimensions:
        index.error = "no usable embedding blob found"
    return index


def decode_embedding(blob: bytes) -> list[float]:
    """Decode one stored embedding (``<Nf`` float32, little endian)."""
    if len(blob) % 4:
        raise ValueError("embedding blob is not a whole number of float32 values")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _target_revision_status(
    *, target_db: str, tenant_id: str, model_revision_id: str
) -> tuple[bool, bool]:
    """``(published, granted)`` for one model revision, from the enterprise side.

    ``granted`` asks the tenant-level question (does the revision carry the
    tenant-wide approval?), not the caller-scoped reach the workflow resolver
    answers: a revision only one department holds is not approved for migration.
    """
    from octop.infra.db.pool import PostgresPool
    from octop.infra.db.repos.workbuddy_catalog import CAPABILITY_MODEL, WorkBuddyCatalogRepo
    from octop.infra.db.repos.workbuddy_knowledge import WorkBuddyKnowledgeRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext

    pool = PostgresPool(target_db, min_size=1, max_size=1)
    try:
        repo = WorkBuddyKnowledgeRepo(pool)
        ctx = WorkBuddyDbContext.for_tenant(tenant_id)
        revision = repo.get_platform_model_revision(ctx, model_revision_id)
        published = revision is not None and str(revision.status) == "published"
        granted = WorkBuddyCatalogRepo(pool).granted_tenant_wide(
            tenant_id, kind=CAPABILITY_MODEL, revision_id=model_revision_id
        )
        return published, granted
    finally:
        pool.close()


def classify(
    base: SourceBase,
    index: SourceIndex,
    *,
    target_checked: bool,
    revision_published: bool,
    revision_granted: bool,
) -> tuple[str, str, list[str]]:
    """The import path for one base, with the reason and any caveats."""
    notes: list[str] = []
    if index.error:
        return PATH_SUSPEND, f"source index is unusable ({index.error})", notes
    if index.chunks == 0:
        return PATH_SUSPEND, "the source base holds no chunks", notes
    seen = index.dimension
    if seen is None:
        return PATH_SUSPEND, "stored vectors disagree on their dimension", notes
    if base.embedding_dim and base.embedding_dim != seen:
        notes.append(f"metadata says {base.embedding_dim} dimensions but the vectors hold {seen}")
    if not target_checked:
        return PATH_UNKNOWN, "no target database was given, so authorization is unchecked", notes
    if not revision_published:
        return PATH_SUSPEND, "the pinned model revision is not published in the catalog", notes
    if not revision_granted:
        return PATH_SUSPEND, "the tenant has not been granted the model revision", notes
    if seen != ENTERPRISE_DIMENSIONS:
        return (
            PATH_REEMBED,
            f"stored vectors have {seen} dimensions, the model produces {ENTERPRISE_DIMENSIONS}",
            notes,
        )
    return PATH_COPY, "the model revision is usable and the vectors already match", notes


def analyse(
    *,
    source_dir: Path,
    source_db: Path,
    target_db: str | None,
    tenant_id: str | None,
    model_revision_id: str | None,
) -> dict[str, Any]:
    """Build the migration report for every base the source describes."""
    target_checked = bool(target_db and tenant_id and model_revision_id)
    revision_published = revision_granted = False
    target_error = ""
    if target_checked:
        try:
            revision_published, revision_granted = _target_revision_status(
                target_db=str(target_db),
                tenant_id=str(tenant_id),
                model_revision_id=str(model_revision_id),
            )
        except Exception as exc:  # the report must survive an unreachable target
            target_checked = False
            target_error = f"{type(exc).__name__}: {exc}"

    report: dict[str, Any] = {
        "source_dir": str(source_dir),
        "source_db": str(source_db),
        "target": {
            "database": target_db or "",
            "tenant_id": tenant_id or "",
            "model_revision_id": model_revision_id or "",
            "checked": target_checked,
            "error": target_error,
        },
        "import_policy": {
            "document_source": IMPORT_DOCUMENT_SOURCE,
            "file_reference": "none: the personal edition kept no original file",
            "apply": "not in this batch (B-10/B-12/B-15 import the content)",
        },
        "knowledge_bases": [],
        "errors": [],
    }

    try:
        bases = read_source_bases(source_db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        report["errors"].append(f"source control plane: {exc}")
        bases = []

    totals = {PATH_COPY: 0, PATH_REEMBED: 0, PATH_SUSPEND: 0, PATH_UNKNOWN: 0}
    for base in bases:
        index = read_source_index(source_dir / base.kb_id / "index.sqlite")
        path, reason, notes = classify(
            base,
            index,
            target_checked=target_checked,
            revision_published=revision_published,
            revision_granted=revision_granted,
        )
        totals[path] = totals.get(path, 0) + 1
        if path == PATH_COPY and base.embedding_dim and base.embedding_dim != index.dimension:
            notes.append("vectors kept as they are, despite the metadata mismatch")
        entry: dict[str, Any] = {
            "kb_id": base.kb_id,
            "name": base.name,
            "owner_user_id": base.owner_user_id,
            "target_scope": base.target_scope,
            "target_acl": "none (scope carries the personal/enterprise layer)",
            "default_open": "user preference",
            "documents": index.documents,
            "chunks": index.chunks,
            "source_model": base.embedding_model,
            "source_dim": base.embedding_dim,
            "vector_dim": index.dimension,
            "path": path,
            "reason": reason,
            "notes": notes,
        }
        report["knowledge_bases"].append(entry)

    report["totals"] = {
        "knowledge_bases": len(bases),
        "documents": sum(entry["documents"] for entry in report["knowledge_bases"]),
        "chunks": sum(entry["chunks"] for entry in report["knowledge_bases"]),
        "by_path": totals,
    }
    return report


def _summarise(report: dict[str, Any]) -> str:
    lines = [
        f"source: {report['source_db']} + {report['source_dir']}",
        f"target: {report['target']['database'] or '(none)'} checked={report['target']['checked']}",
    ]
    if report["target"]["error"]:
        lines.append(f"target error: {report['target']['error']}")
    lines.append("path  documents  chunks  base")
    for entry in report["knowledge_bases"]:
        lines.append(
            f"  {entry['path']:>4}  {entry['documents']:>9}  {entry['chunks']:>6}  {entry['name']}"
            f"  [{entry['reason']}]"
        )
    totals = report["totals"]
    lines.append(
        f"totals: {totals['knowledge_bases']} bases, {totals['documents']} documents, "
        f"{totals['chunks']} chunks, by path {totals['by_path']}"
    )
    for error in report["errors"]:
        lines.append(f"error: {error}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the personal edition's knowledge bases with the enterprise "
            "catalog and report an import path per base. Read-only: this batch "
            "neither writes to the source nor imports anything."
        )
    )
    parser.add_argument(
        "--source-dir",
        default=str(Path.home() / ".octop" / "knowledge"),
        help="directory holding one <kb_id>/index.sqlite per base",
    )
    parser.add_argument(
        "--source-db",
        default=str(Path.home() / ".octop" / "octop.db"),
        help="the personal control-plane SQLite database (knowledge_bases table)",
    )
    parser.add_argument(
        "--target-db",
        default="",
        help=(
            "enterprise PostgreSQL DSN; without it no authorization can be checked "
            "and every base is reported as 'unknown'"
        ),
    )
    parser.add_argument("--tenant-id", default="", help="enterprise tenant to check grants for")
    parser.add_argument(
        "--model-revision-id",
        default="",
        help="platform model revision the imported base should pin",
    )
    parser.add_argument("--report", default="", help="write the report as JSON to this path")
    parser.add_argument("--quiet", action="store_true", help="write only the JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = analyse(
        source_dir=Path(args.source_dir).expanduser(),
        source_db=Path(args.source_db).expanduser(),
        target_db=args.target_db.strip() or None,
        tenant_id=args.tenant_id.strip() or None,
        model_revision_id=args.model_revision_id.strip() or None,
    )
    if args.report:
        Path(args.report).expanduser().write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    if not args.quiet:
        print(_summarise(report))
    else:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    # A source that cannot be read is a failure the operator must act on; bases
    # that need authorization are reported, not fatal.
    return 1 if report["errors"] else 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    sys.exit(main())

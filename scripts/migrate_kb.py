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

Nothing here writes to the source, and the default run does not write to the
target either: the report is the deliverable (``--report`` writes it as JSON, and
the same summary goes to stdout).  Idempotent by construction — re-running only
re-reads, and the analysis is a pure function of the two sources.

``--apply`` (B-13) adds the **mapping** half, and writes to the target only:

    * the tenant base row, with the personal ``shared`` flag as its scope —
      ``shared`` becomes a company base (scope ``enterprise``, and no owner, which
      is the shape ``wb_knowledge_bases_scope_shape`` allows), everything else
      stays personal with the source owner;
    * the owner's ``default_open`` preference (``workbuddy_knowledge_preferences``,
      which is per member in a tenant);
    * one explicit grant per residual ``knowledge_base_members`` row of a pre-v7
      source, because the personal edition dropped that table without migrating
      its rows.  The grant is written to *both* ACL tables a knowledge base has
      today — ``workbuddy_knowledge_acl`` and the generic
      ``workbuddy_object_acl`` of the four-layer model (``050_workbuddy_object_rbac``).

``workbuddy_knowledge_acl`` is the one that decides visibility: the knowledge
service (``infra/workbuddy/knowledge.py``) resolves a member's read of a base from
the base's own ``workbuddy_knowledge_acl`` rows, so a grant that only lands in the
generic table reaches the resolver but not the knowledge pages.  ``object_acl`` is
written as well so the two models do not drift while the platform converges on the
generic one; a grant in ``workbuddy_object_acl`` additionally requires its object
to be registered in ``workbuddy_object_scopes`` (the table's foreign key), which
this script does with the base's own scope shape.

The tenant twin of a base is created by
:func:`octop.infra.workbuddy.knowledge_adapter.mirror_base`, the same function the
running server mirrors personal writes with, so "the tenant twin of a personal
base" has exactly one definition.  Re-running changes nothing: the base is
matched by creator and name, and the preference and the grants are upserts.  A
base whose owner is not an active member of any tenant — or whose owner resolves
to a tenant other than the one ``--tenant-id`` names — is reported and skipped,
never fatal.  Document content still moves with B-10/B-12: this batch maps the
containers and their permissions.

Usage:

    python scripts/migrate_kb.py                       # analyse ~/.octop against $OCTOP_DATABASE_URL
    python scripts/migrate_kb.py --source-dir /backup/knowledge --source-db /backup/octop.db \
        --target-db postgresql://user:pass@host/db --tenant-id <uuid> --model-revision-id <uuid> \
        --report migration-report.json
    python scripts/migrate_kb.py --source-db /backup/octop.db --target-db postgresql://host/db \
        --tenant-id <uuid> --apply --report mapping-report.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

#: The enterprise knowledge tables fix the embedding dimension at 1024.
ENTERPRISE_DIMENSIONS = 1024

#: The kind a knowledge base is registered under in the generic permission
#: tables (``infra/rbac/model.py::ObjectKind``).
OBJECT_KIND_KNOWLEDGE_BASE = "knowledge_base"

#: Personal-edition member roles as enterprise permissions.  The dropped table
#: carried a free-form ``role`` with no CHECK constraint, and the only value this
#: repository ever wrote was ``viewer``; anything unrecognised falls back to
#: ``read``, the least privilege that keeps the member's reach (B-13).
MEMBER_PERMISSION_BY_ROLE = {
    "viewer": "read",
    "read": "read",
    "read_only": "read",
    "editor": "write",
    "writer": "write",
    "write": "write",
    "contributor": "write",
    "owner": "admin",
    "admin": "admin",
}
DEFAULT_MEMBER_PERMISSION = "read"

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
    description: str = ""
    max_documents: int | None = None

    @property
    def target_scope(self) -> str:
        """``shared`` meant "visible to everyone" in the personal edition."""
        return "enterprise" if self.shared else "personal"

    @property
    def target_owner_user_id(self) -> int | None:
        """The tenant row's owner: a company base carries none (shape constraint)."""
        return None if self.shared else self.owner_user_id


@dataclass(slots=True)
class SourceMember:
    """One residual ``knowledge_base_members`` row of a pre-v7 personal install."""

    kb_id: str
    user_id: int
    permission: str


def member_permission(role: Any) -> str:
    """The enterprise permission a personal-edition member ``role`` means."""
    return MEMBER_PERMISSION_BY_ROLE.get(str(role or "").strip().lower(), DEFAULT_MEMBER_PERMISSION)


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


def _base_columns(connection: sqlite3.Connection) -> list[str]:
    """The base columns the source has.

    ``description`` has been there since the table was created and
    ``max_documents`` only since schema v10; a source older than either is still
    a valid source, so the SELECT asks for what is present.
    """
    present = {str(row["name"]) for row in connection.execute("PRAGMA table_info(knowledge_bases)")}
    columns = [
        "knowledge_base_id",
        "name",
        "owner_user_id",
        "shared",
        "default_open",
        "embedding_model",
        "embedding_dim",
        "doc_count",
    ]
    columns.extend(optional for optional in ("description", "max_documents") if optional in present)
    return columns


def _source_base(row: sqlite3.Row, present: frozenset[str]) -> SourceBase:
    """One base row, with the columns an older source may not carry defaulted."""
    cap = int(row["max_documents"]) if "max_documents" in present else None
    return SourceBase(
        kb_id=str(row["knowledge_base_id"]),
        name=str(row["name"]),
        owner_user_id=int(row["owner_user_id"]),
        shared=bool(row["shared"]),
        default_open=bool(row["default_open"]),
        embedding_model=str(row["embedding_model"] or ""),
        embedding_dim=int(row["embedding_dim"] or 0),
        doc_count=int(row["doc_count"] or 0),
        description=str(row["description"] or "") if "description" in present else "",
        max_documents=cap,
    )


def read_source_bases(db_path: Path) -> list[SourceBase]:
    """Every base in the personal control plane, oldest first."""
    if not db_path.exists():
        raise FileNotFoundError(f"personal control-plane database not found: {db_path}")
    with _connect_readonly(db_path) as connection:
        columns = _base_columns(connection)
        rows = connection.execute(
            f"SELECT {', '.join(columns)} FROM knowledge_bases "
            "ORDER BY created_at, knowledge_base_id"
        ).fetchall()
    present = frozenset(columns)
    return [_source_base(row, present) for row in rows]


def read_source_members(db_path: Path) -> list[SourceMember]:
    """Residual ``knowledge_base_members`` rows, empty when the table is gone.

    Schema v7 dropped that table (``007_resource_identity_and_profile``) without
    moving its rows anywhere, so only a source that never reached v7 still has
    it.  A missing table is the normal case, not an error.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"personal control-plane database not found: {db_path}")
    with _connect_readonly(db_path) as connection:
        present = connection.execute(
            "SELECT 1 AS present FROM sqlite_master "
            "WHERE type = 'table' AND name = 'knowledge_base_members'"
        ).fetchone()
        if present is None:
            return []
        rows = connection.execute(
            "SELECT kb_id, user_id, role FROM knowledge_base_members ORDER BY kb_id, user_id"
        ).fetchall()
    return [
        SourceMember(
            kb_id=str(row["kb_id"]),
            user_id=int(row["user_id"]),
            permission=member_permission(row["role"]),
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
    apply: bool = False,
) -> dict[str, Any]:
    """Build the migration report for every base the source describes.

    ``apply`` only states the mode in the report; the writes themselves live in
    :func:`apply_mapping`, so the analysis stays a pure function of the sources.
    """
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
            "apply": (
                "written by --apply: the base row, the owner's default-open preference "
                "and the residual member grants (B-13); document content is imported by "
                "B-10/B-12"
                if apply
                else "not in this batch (B-10/B-12 import the content; --apply maps the "
                "containers and permissions)"
            ),
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
            "target_acl": "residual knowledge_base_members rows only (written by --apply)",
            "default_open": base.default_open,
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


# ── the mapping half: bases, preferences, residual grants (B-13) ─────────────


def _mirror_host(pool: Any) -> Any:
    """A server-shaped object for the adapter: only ``.services.db`` is read.

    ``mirror_base`` was written for the running server and is reused here on
    purpose — the migration must not grow a second definition of the tenant twin
    of a personal base.
    """
    from octop.config import OctopConfig
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    return SimpleNamespace(
        services=build_shared_services(db=pool, paths=PathLayout.from_env(), config=OctopConfig())
    )


def apply_base(
    *,
    pool: Any,
    host: Any,
    base: SourceBase,
    members: Sequence[SourceMember] = (),
    tenant_pin: str | None = None,
) -> dict[str, Any]:
    """Map one personal base onto the tenant tables; returns its report entry.

    Every step is an upsert, so a second run over the same base changes nothing:
    the base is matched by creator and name, the preference and the grants are
    re-granted in place.  A base that cannot be mapped is *reported*, never
    raised: the operator decides whether to grant a model revision or to import
    that owner into the tenant first.
    """
    from octop.infra.db.repos.workbuddy_knowledge import WorkBuddyKnowledgeRepo
    from octop.infra.rbac.repo import WorkBuddyRbacRepo
    from octop.infra.workbuddy.knowledge_adapter import mirror_base, mirror_target

    ctx = mirror_target(host, base.owner_user_id)
    if ctx is None:
        return {"skipped": f"owner {base.owner_user_id} is not an active member of any tenant"}
    tenant_id = str(ctx.tenant_id)
    if tenant_pin and tenant_id != tenant_pin:
        return {
            "tenant_id": tenant_id,
            "skipped": f"owner belongs to tenant {tenant_id}, not the requested {tenant_pin}",
        }

    repo = WorkBuddyKnowledgeRepo(pool)
    known = {row.kb_id for row in repo.list_bases(ctx)}
    kb_id = mirror_base(host, user_id=base.owner_user_id, base=base)
    if kb_id is None:
        return {
            "tenant_id": tenant_id,
            "skipped": (
                "not mirrored: the tenant has no granted embedding revision (the adapter "
                "logs which one is missing), or the insert was refused"
            ),
        }

    entry: dict[str, Any] = {
        "tenant_id": tenant_id,
        "kb_id": kb_id,
        "base": "matched" if kb_id in known else "created",
        "preference": "not requested",
        "acl": {"granted": 0, "updated": 0, "unchanged": 0, "skipped": 0, "skipped_user_ids": []},
        "skipped": "",
    }
    if base.default_open:
        opened = repo.set_default_open(ctx, kb_id, user_id=base.owner_user_id, default_open=True)
        entry["preference"] = "written" if opened else "refused"
    if members:
        rbac = WorkBuddyRbacRepo(pool)
        # workbuddy_object_acl references workbuddy_object_scopes, so the object is
        # registered first; its scope is the base's own shape, which is what the
        # four-layer model resolves (personal names the owner, enterprise names none).
        rbac.set_scope(
            ctx,
            object_kind=OBJECT_KIND_KNOWLEDGE_BASE,
            object_id=kb_id,
            scope=base.target_scope,
            owner_user_id=base.target_owner_user_id,
            department_id=None,
            created_by_user_id=base.owner_user_id,
        )
        # The same grant lives in two tables: the generic four-layer one and the
        # knowledge base's own ``workbuddy_knowledge_acl``, which is the table the
        # knowledge service resolves visibility with (module docstring).  The base
        # table has no uniqueness on ``(kb_id, user_id)``, so the existing rows are
        # read first and every subject is granted in place instead of inserted twice.
        generic = {
            int(row.user_id): row.permission
            for row in rbac.list_grants(ctx, OBJECT_KIND_KNOWLEDGE_BASE, kb_id)
            if row.user_id is not None
        }
        granted = {
            int(row.user_id): row for row in repo.list_acl(ctx, kb_id) if row.user_id is not None
        }
        for member in members:
            if not rbac.member_exists(ctx, member.user_id):
                entry["acl"]["skipped"] += 1
                entry["acl"]["skipped_user_ids"].append(member.user_id)
                continue
            created = member.user_id not in generic
            updated = not created and generic[member.user_id] != member.permission
            if created or updated:
                rbac.upsert_grant(
                    ctx,
                    object_kind=OBJECT_KIND_KNOWLEDGE_BASE,
                    object_id=kb_id,
                    permission=member.permission,
                    user_id=member.user_id,
                    department_id=None,
                    granted_by_user_id=base.owner_user_id,
                )
            current = granted.get(member.user_id)
            if current is None:
                created = True
                repo.add_acl(
                    ctx,
                    kb_id,
                    permission=member.permission,
                    granted_by_user_id=base.owner_user_id,
                    user_id=member.user_id,
                )
            elif current.permission != member.permission:
                updated = True
                repo.update_acl_permission(ctx, kb_id, current.acl_id, permission=member.permission)
            if created:
                entry["acl"]["granted"] += 1
            elif updated:
                entry["acl"]["updated"] += 1
            else:
                entry["acl"]["unchanged"] += 1
    return entry


def apply_mapping(
    *,
    source_db: Path,
    target_db: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Map every base the source describes; returns the per-base outcomes.

    ``{"bases": {kb_id: outcome}, "mapped": n, "skipped": n, "preferences": n,
    "acl_written": n, "acl_skipped": n, "errors": [...]}`` — one base that cannot
    be mapped does not stop the others, and the errors are what makes the caller's
    exit code non-zero.
    """
    from octop.infra.db.pool import PostgresPool

    outcome: dict[str, Any] = {
        "bases": {},
        "mapped": 0,
        "skipped": 0,
        "preferences": 0,
        "acl_granted": 0,
        "acl_updated": 0,
        "acl_unchanged": 0,
        "acl_skipped": 0,
        "errors": [],
    }
    try:
        bases = read_source_bases(source_db)
        members = read_source_members(source_db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        outcome["errors"].append(f"mapping: {exc}")
        return outcome

    by_base: dict[str, list[SourceMember]] = {}
    for member in members:
        by_base.setdefault(member.kb_id, []).append(member)

    pool = PostgresPool(target_db, min_size=1, max_size=1)
    try:
        host = _mirror_host(pool)
        for base in bases:
            try:
                entry = apply_base(
                    pool=pool,
                    host=host,
                    base=base,
                    members=by_base.get(base.kb_id, ()),
                    tenant_pin=tenant_id,
                )
            except Exception as exc:  # one unusable base must not stop the rest
                entry = {"error": f"{type(exc).__name__}: {exc}"}
                outcome["errors"].append(f"mapping {base.kb_id}: {entry['error']}")
            outcome["bases"][base.kb_id] = entry
            if entry.get("skipped") or entry.get("error"):
                outcome["skipped"] += 1
                continue
            outcome["mapped"] += 1
            outcome["preferences"] += 1 if entry["preference"] == "written" else 0
            outcome["acl_granted"] += int(entry["acl"]["granted"])
            outcome["acl_updated"] += int(entry["acl"]["updated"])
            outcome["acl_unchanged"] += int(entry["acl"]["unchanged"])
            outcome["acl_skipped"] += int(entry["acl"]["skipped"])
    finally:
        pool.close()
    return outcome


def _mapping_totals(mapping: dict[str, Any]) -> dict[str, Any]:
    """The mapping section of the report: everything but the per-base map."""
    return {key: value for key, value in mapping.items() if key != "bases"}


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
    mapping = report.get("mapping")
    if mapping:
        lines.append(
            f"mapping: {mapping['mapped']} mapped, {mapping['skipped']} skipped, "
            f"{mapping['preferences']} preferences, acl {mapping['acl_granted']} granted / "
            f"{mapping['acl_updated']} updated / {mapping['acl_unchanged']} unchanged "
            f"({mapping['acl_skipped']} subjects outside the tenant)"
        )
        for entry in report["knowledge_bases"]:
            mapped = entry.get("apply") or {}
            if mapped and (mapped.get("skipped") or mapped.get("error")):
                lines.append(
                    f"  not mapped: {entry['name']} [{mapped.get('skipped') or mapped['error']}]"
                )
    for error in report["errors"]:
        lines.append(f"error: {error}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile the personal edition's knowledge bases with the enterprise "
            "catalog and report an import path per base. Without --apply nothing is "
            "written anywhere; --apply maps the bases, the owner preferences and the "
            "residual member grants into the target tenant (idempotent)."
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
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "write the mapping into --target-db: the base row's scope and owner, the "
            "owner's default-open preference, and one grant per residual "
            "knowledge_base_members row (workbuddy_knowledge_acl and "
            "workbuddy_object_acl); re-running changes nothing"
        ),
    )
    parser.add_argument("--report", default="", help="write the report as JSON to this path")
    parser.add_argument("--quiet", action="store_true", help="write only the JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source_db = Path(args.source_db).expanduser()
    target_db = args.target_db.strip()
    tenant_id = args.tenant_id.strip() or None
    report = analyse(
        source_dir=Path(args.source_dir).expanduser(),
        source_db=source_db,
        target_db=target_db or None,
        tenant_id=tenant_id,
        model_revision_id=args.model_revision_id.strip() or None,
        apply=args.apply,
    )
    if args.apply:
        if not target_db:
            report["errors"].append(
                "--apply needs --target-db: there is nowhere to write without it"
            )
        else:
            mapping = apply_mapping(source_db=source_db, target_db=target_db, tenant_id=tenant_id)
            for entry in report["knowledge_bases"]:
                entry["apply"] = mapping["bases"].get(
                    entry["kb_id"], {"skipped": "the base was not read from the source"}
                )
            report["mapping"] = _mapping_totals(mapping)
            report["errors"].extend(mapping["errors"])
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

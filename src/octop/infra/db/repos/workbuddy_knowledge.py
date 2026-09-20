"""PostgreSQL access for WorkBuddy knowledge bases, documents and triggers.

Every statement runs inside
:func:`octop.infra.db.workbuddy_context.workbuddy_transaction`, so row level
security scopes all rows to the transaction-local tenant and a SQLite control
plane fails closed (``WORKBUDDY_POSTGRES_REQUIRED``) before a single query runs.

Layout follows migration ``019_workbuddy_knowledge_triggers``:

* ``workbuddy_knowledge_bases`` / ``_acl``    — scoped bases and additive grants
* ``workbuddy_knowledge_uploads`` / ``_file_refs`` — bound upload handshakes
* ``workbuddy_knowledge_documents`` / ``_generations`` / ``_chunks`` — retrieval
* ``workbuddy_trigger_registrations`` / ``_grants`` / ``_deliveries`` — triggers

Authorization itself (scope precedence, ACL additions) lives in
``octop.infra.workbuddy.knowledge``; this module only reads and writes rows.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    workbuddy_transaction,
)
from octop.infra.rbac.subjects import (
    SUBJECT_DEPARTMENT_CHAIN,
    grant_subject_reach,
)

MAX_LIST_BASES = 200
MAX_LIST_DOCUMENTS = 200
MAX_LIST_DELIVERIES = 100

#: pgvector column width pinned by migration 019 and the bge-m3 model contract.
EMBEDDING_DIMENSIONS = 1024


def new_uuid() -> str:
    """Public UUID for a WorkBuddy row (never a database-generated default here)."""
    return str(uuid.uuid4())


def _int_or_none(row: DbRow, key: str) -> int | None:
    value = row[key]
    return None if value is None else int(value)


def _folder_path_or_root(row: DbRow) -> str:
    """Schema v55 adds ``folder_path``; older rows and doubles mean the base root."""
    keys = frozenset(row.keys()) if hasattr(row, "keys") else frozenset()
    if "folder_path" not in keys:
        return ""
    return str(row["folder_path"] or "")


def _str_or_none(row: DbRow, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    return {}


def vector_literal(embedding: list[float]) -> str:
    """pgvector text literal for a bound parameter (``[0.1,0.2,...]``)."""
    return "[" + ",".join(repr(float(value)) for value in embedding) + "]"


@dataclass(frozen=True, slots=True)
class WorkBuddyPlatformModelRevisionRow:
    model_revision_id: str
    adapter_key: str
    model_key: str
    revision: int
    display_name: str
    status: str

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyPlatformModelRevisionRow:
        return cls(
            model_revision_id=str(row["model_revision_id"]),
            adapter_key=str(row["adapter_key"]),
            model_key=str(row["model_key"]),
            revision=int(row["revision"]),
            display_name=str(row["display_name"]),
            status=str(row["status"]),
        )


# The per-base cap the personal edition has enforced since v10, and the ceiling
# the ingestion path will accept (schema v54 holds the same bound).
MAX_DOCUMENTS_PER_BASE = 100000
DEFAULT_MAX_DOCUMENTS = 100


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeBaseRow:
    kb_id: str
    tenant_id: str
    scope: str
    owner_user_id: int | None
    department_id: str | None
    name: str
    description: str
    embedding_model_revision_id: str
    embedding_adapter_key: str
    embedding_model_key: str
    embedding_revision: int
    embedding_dimensions: int
    archived_at: int | None
    created_by_user_id: int
    created_at: int
    updated_at: int
    # Schema v54 adds the per-base cap. Callers that build the row by hand get the
    # personal edition's default, which is what a base created before v54 holds.
    max_documents: int = DEFAULT_MAX_DOCUMENTS

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeBaseRow:
        return cls(
            kb_id=str(row["kb_id"]),
            tenant_id=str(row["tenant_id"]),
            scope=str(row["scope"]),
            owner_user_id=_int_or_none(row, "owner_user_id"),
            department_id=_str_or_none(row, "department_id"),
            name=str(row["name"]),
            description=str(row["description"]),
            embedding_model_revision_id=str(row["embedding_model_revision_id"]),
            embedding_adapter_key=str(row["embedding_adapter_key"]),
            embedding_model_key=str(row["embedding_model_key"]),
            embedding_revision=int(row["embedding_revision"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
            max_documents=int(row["max_documents"]),
            archived_at=_int_or_none(row, "archived_at"),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeAclRow:
    acl_id: str
    tenant_id: str
    kb_id: str
    user_id: int | None
    department_id: str | None
    permission: str
    granted_by_user_id: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeAclRow:
        return cls(
            acl_id=str(row["acl_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_id=str(row["kb_id"]),
            user_id=_int_or_none(row, "user_id"),
            department_id=_str_or_none(row, "department_id"),
            permission=str(row["permission"]),
            granted_by_user_id=int(row["granted_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeUploadRow:
    upload_id: str
    tenant_id: str
    kb_id: str
    requested_by_user_id: int
    filename: str
    mime_type: str
    size_bytes: int
    object_key: str
    status: str
    checksum_sha256: str | None
    detected_mime: str | None
    scan_status: str | None
    file_ref_id: str | None
    rejection_code: str | None
    created_at: int
    expires_at: int
    completed_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeUploadRow:
        return cls(
            upload_id=str(row["upload_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_id=str(row["kb_id"]),
            requested_by_user_id=int(row["requested_by_user_id"]),
            filename=str(row["filename"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            object_key=str(row["object_key"]),
            status=str(row["status"]),
            checksum_sha256=_str_or_none(row, "checksum_sha256"),
            detected_mime=_str_or_none(row, "detected_mime"),
            scan_status=_str_or_none(row, "scan_status"),
            file_ref_id=_str_or_none(row, "file_ref_id"),
            rejection_code=_str_or_none(row, "rejection_code"),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
            completed_at=_int_or_none(row, "completed_at"),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeFileRefRow:
    file_ref_id: str
    tenant_id: str
    kb_id: str
    upload_id: str
    object_key: str
    filename: str
    mime_type: str
    size_bytes: int
    checksum_sha256: str
    created_by_user_id: int
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeFileRefRow:
        return cls(
            file_ref_id=str(row["file_ref_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_id=str(row["kb_id"]),
            upload_id=str(row["upload_id"]),
            object_key=str(row["object_key"]),
            filename=str(row["filename"]),
            mime_type=str(row["mime_type"]),
            size_bytes=int(row["size_bytes"]),
            checksum_sha256=str(row["checksum_sha256"]),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeDocumentRow:
    document_id: str
    tenant_id: str
    kb_id: str
    file_ref_id: str | None
    source: str
    title: str
    status: str
    error_code: str | None
    active_generation_id: str | None
    chunk_count: int
    job_id: str
    created_by_user_id: int
    created_at: int
    updated_at: int
    deleted_at: int | None
    # Schema v55 adds the folder path. Rows read before it existed (and callers
    # that build the row by hand) mean the base root.
    folder_path: str = ""

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeDocumentRow:
        return cls(
            document_id=str(row["document_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_id=str(row["kb_id"]),
            file_ref_id=_str_or_none(row, "file_ref_id"),
            source=str(row["source"]),
            title=str(row["title"]),
            status=str(row["status"]),
            error_code=_str_or_none(row, "error_code"),
            active_generation_id=_str_or_none(row, "active_generation_id"),
            chunk_count=int(row["chunk_count"]),
            job_id=str(row["job_id"]),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            deleted_at=_int_or_none(row, "deleted_at"),
            folder_path=_folder_path_or_root(row),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeGenerationRow:
    generation_id: str
    tenant_id: str
    kb_id: str
    document_id: str
    generation_number: int
    status: str
    embedding_model_revision_id: str
    embedding_adapter_key: str
    embedding_model_key: str
    embedding_revision: int
    embedding_dimensions: int
    chunk_count: int
    error_code: str | None
    created_by_user_id: int
    created_at: int
    ready_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeGenerationRow:
        return cls(
            generation_id=str(row["generation_id"]),
            tenant_id=str(row["tenant_id"]),
            kb_id=str(row["kb_id"]),
            document_id=str(row["document_id"]),
            generation_number=int(row["generation_number"]),
            status=str(row["status"]),
            embedding_model_revision_id=str(row["embedding_model_revision_id"]),
            embedding_adapter_key=str(row["embedding_adapter_key"]),
            embedding_model_key=str(row["embedding_model_key"]),
            embedding_revision=int(row["embedding_revision"]),
            embedding_dimensions=int(row["embedding_dimensions"]),
            chunk_count=int(row["chunk_count"]),
            error_code=_str_or_none(row, "error_code"),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
            ready_at=_int_or_none(row, "ready_at"),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeChunkHit:
    chunk_id: str
    document_id: str
    document_title: str
    generation_id: str
    ordinal: int
    content: str
    token_count: int
    metadata: dict[str, Any]
    score: float

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyKnowledgeChunkHit:
        return cls(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            document_title=str(row["document_title"]),
            generation_id=str(row["generation_id"]),
            ordinal=int(row["ordinal"]),
            content=str(row["content"]),
            token_count=int(row["token_count"]),
            metadata=_json_object(row["metadata"]),
            score=float(row["score"]),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyTriggerRegistrationRow:
    registration_id: str
    tenant_id: str
    workflow_id: str
    kind: str
    name: str
    enabled: bool
    webhook_path: str | None
    cron_expression: str | None
    event_name: str | None
    event_filter: dict[str, Any]
    secret_provider: str | None
    secret_ref: str | None
    secret_version: int
    previous_secret_ref: str | None
    previous_secret_expires_at: int | None
    signature_algorithm: str
    signature_header: str
    timestamp_header: str
    tolerance_seconds: int
    created_by_user_id: int
    created_at: int
    updated_at: int
    revoked_at: int | None

    @property
    def is_active(self) -> bool:
        return self.enabled and self.revoked_at is None

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyTriggerRegistrationRow:
        return cls(
            registration_id=str(row["registration_id"]),
            tenant_id=str(row["tenant_id"]),
            workflow_id=str(row["workflow_id"]),
            kind=str(row["kind"]),
            name=str(row["name"]),
            enabled=bool(row["enabled"]),
            webhook_path=_str_or_none(row, "webhook_path"),
            cron_expression=_str_or_none(row, "cron_expression"),
            event_name=_str_or_none(row, "event_name"),
            event_filter=_json_object(row["event_filter"]),
            secret_provider=_str_or_none(row, "secret_provider"),
            secret_ref=_str_or_none(row, "secret_ref"),
            secret_version=int(row["secret_version"]),
            previous_secret_ref=_str_or_none(row, "previous_secret_ref"),
            previous_secret_expires_at=_int_or_none(row, "previous_secret_expires_at"),
            signature_algorithm=str(row["signature_algorithm"]),
            signature_header=str(row["signature_header"]),
            timestamp_header=str(row["timestamp_header"]),
            tolerance_seconds=int(row["tolerance_seconds"]),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            revoked_at=_int_or_none(row, "revoked_at"),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyTriggerGrantRow:
    grant_id: str
    tenant_id: str
    registration_id: str
    capability: str
    tool_name: str | None
    kb_id: str | None
    permission: str | None
    created_by_user_id: int
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyTriggerGrantRow:
        return cls(
            grant_id=str(row["grant_id"]),
            tenant_id=str(row["tenant_id"]),
            registration_id=str(row["registration_id"]),
            capability=str(row["capability"]),
            tool_name=_str_or_none(row, "tool_name"),
            kb_id=_str_or_none(row, "kb_id"),
            permission=_str_or_none(row, "permission"),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyTriggerDeliveryRow:
    delivery_id: str
    tenant_id: str
    registration_id: str
    event_key: str
    event_name: str | None
    status: str
    is_test: bool
    body_sha256: str
    signature_version: int | None
    signature_timestamp: int | None
    attempt: int
    execution_id: str | None
    rejection_code: str | None
    actor_user_id: int | None
    received_at: int
    completed_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyTriggerDeliveryRow:
        return cls(
            delivery_id=str(row["delivery_id"]),
            tenant_id=str(row["tenant_id"]),
            registration_id=str(row["registration_id"]),
            event_key=str(row["event_key"]),
            event_name=_str_or_none(row, "event_name"),
            status=str(row["status"]),
            is_test=bool(row["is_test"]),
            body_sha256=str(row["body_sha256"]),
            signature_version=_int_or_none(row, "signature_version"),
            signature_timestamp=_int_or_none(row, "signature_timestamp"),
            attempt=int(row["attempt"]),
            execution_id=_str_or_none(row, "execution_id"),
            rejection_code=_str_or_none(row, "rejection_code"),
            actor_user_id=_int_or_none(row, "actor_user_id"),
            received_at=int(row["received_at"]),
            completed_at=_int_or_none(row, "completed_at"),
        )


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    """Outcome of the persistent event-key claim.

    ``dispatch`` is True only for the caller that owns the event key; a duplicate
    (or a delivery that already executed) returns the existing row untouched.
    """

    delivery: WorkBuddyTriggerDeliveryRow
    dispatch: bool


_BASE_COLUMNS = (
    "kb_id, tenant_id, scope, owner_user_id, department_id, name, description, "
    "embedding_model_revision_id, embedding_adapter_key, embedding_model_key, "
    "embedding_revision, embedding_dimensions, max_documents, archived_at, "
    "created_by_user_id, created_at, updated_at"
)


class WorkBuddyKnowledgeRepo:
    """SQL for knowledge bases, bound uploads, generations, chunks and pins."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ── embedding model pin ────────────────────────────────────────────────

    def get_platform_model_revision(
        self, ctx: WorkBuddyDbContext, model_revision_id: str
    ) -> WorkBuddyPlatformModelRevisionRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT model_revision_id, adapter_key, model_key, revision, display_name, status "
                "FROM workbuddy_platform_model_revisions WHERE model_revision_id = ?",
                (model_revision_id,),
            ).fetchone()
        return WorkBuddyPlatformModelRevisionRow.from_row(row) if row is not None else None

    def tenant_model_granted(self, ctx: WorkBuddyDbContext, model_revision_id: str) -> bool:
        """True when the revision reaches the calling member.

        A grant is tenant-wide, for the member's department, or for the member
        itself; a context without a user cannot prove reach and fails closed.
        """
        if ctx.user_id is None:
            return False
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                SUBJECT_DEPARTMENT_CHAIN + " "
                "SELECT 1 AS granted FROM workbuddy_tenant_model_grants g "
                "WHERE g.tenant_id = ? AND g.model_revision_id = ? "
                f"AND {grant_subject_reach('g')}",
                (
                    ctx.tenant_id,
                    ctx.user_id,
                    ctx.tenant_id,
                    ctx.tenant_id,
                    model_revision_id,
                    ctx.user_id,
                ),
            ).fetchone()
        return row is not None

    # ── knowledge bases ────────────────────────────────────────────────────

    def create_base(
        self,
        ctx: WorkBuddyDbContext,
        *,
        scope: str,
        name: str,
        description: str,
        model: WorkBuddyPlatformModelRevisionRow,
        created_by_user_id: int,
        kb_id: str | None = None,
        owner_user_id: int | None = None,
        department_id: str | None = None,
        max_documents: int | None = None,
    ) -> WorkBuddyKnowledgeBaseRow:
        stamp = now_ts()
        public_id = kb_id or new_uuid()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_knowledge_bases ("
                "tenant_id, kb_id, scope, owner_user_id, department_id, name, description, "
                "embedding_model_revision_id, embedding_adapter_key, embedding_model_key, "
                "embedding_revision, embedding_dimensions, max_documents, created_by_user_id, "
                "created_at, updated_at"
                f") VALUES ({', '.join(['?'] * 16)}) "
                f"RETURNING {_BASE_COLUMNS}",
                (
                    ctx.tenant_id,
                    public_id,
                    scope,
                    owner_user_id,
                    department_id,
                    name,
                    description,
                    model.model_revision_id,
                    model.adapter_key,
                    model.model_key,
                    model.revision,
                    EMBEDDING_DIMENSIONS,
                    max_documents or DEFAULT_MAX_DOCUMENTS,
                    created_by_user_id,
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy knowledge base insert returned no row")
        return WorkBuddyKnowledgeBaseRow.from_row(row)

    def get_base(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        include_archived: bool = True,
    ) -> WorkBuddyKnowledgeBaseRow | None:
        clause = "" if include_archived else " AND archived_at IS NULL"
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"SELECT {_BASE_COLUMNS} FROM workbuddy_knowledge_bases "
                f"WHERE tenant_id = ? AND kb_id = ?{clause}",
                (ctx.tenant_id, kb_id),
            ).fetchone()
        return WorkBuddyKnowledgeBaseRow.from_row(row) if row is not None else None

    def list_bases(
        self, ctx: WorkBuddyDbContext, *, limit: int = MAX_LIST_BASES
    ) -> list[WorkBuddyKnowledgeBaseRow]:
        """Tenant rows only; visibility filtering happens in the service."""
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                f"SELECT {_BASE_COLUMNS} FROM workbuddy_knowledge_bases "
                "WHERE tenant_id = ? AND archived_at IS NULL "
                "ORDER BY created_at DESC, kb_id LIMIT ?",
                (ctx.tenant_id, max(1, min(limit, MAX_LIST_BASES))),
            ).fetchall()
        return [WorkBuddyKnowledgeBaseRow.from_row(row) for row in rows]

    def move_document(
        self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str, *, folder_path: str
    ) -> bool:
        """Put one document in a folder; ``False`` when the document is not visible."""
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_documents SET folder_path = ?, updated_at = ?"
                " WHERE tenant_id = ? AND kb_id = ? AND document_id = ? AND deleted_at IS NULL"
                " RETURNING document_id",
                (folder_path, now_ts(), ctx.tenant_id, kb_id, document_id),
            ).fetchone()
        return row is not None

    def rename_document(
        self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str, *, title: str
    ) -> bool:
        """Replace one document's title; ``False`` when the document is not visible."""
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_documents SET title = ?, updated_at = ?"
                " WHERE tenant_id = ? AND kb_id = ? AND document_id = ? AND deleted_at IS NULL"
                " RETURNING document_id",
                (title, now_ts(), ctx.tenant_id, kb_id, document_id),
            ).fetchone()
        return row is not None

    def list_folders(self, ctx: WorkBuddyDbContext, kb_id: str) -> list[tuple[str, int]]:
        """``(folder path, live document count)`` for one base, path order.

        A folder exists exactly as long as it holds a live document — the same
        truth the personal edition kept, without a second table to keep in sync.
        """
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT folder_path, COUNT(*) AS held FROM workbuddy_knowledge_documents"
                " WHERE tenant_id = ? AND kb_id = ? AND deleted_at IS NULL"
                " GROUP BY folder_path ORDER BY folder_path",
                (ctx.tenant_id, kb_id),
            ).fetchall()
        return [(str(row["folder_path"]), int(row["held"])) for row in rows]

    def active_chunk_texts(
        self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str
    ) -> list[str]:
        """The active generation's chunk text in order — the document as indexed.

        A text-only or migrated document keeps no original file: its chunks *are*
        the text, so preview and export read them back here.
        """
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT c.content AS content FROM workbuddy_knowledge_chunks c"
                " JOIN workbuddy_knowledge_documents d"
                " ON d.tenant_id = c.tenant_id AND d.document_id = c.document_id"
                " WHERE c.tenant_id = ? AND c.kb_id = ? AND c.document_id = ?"
                " AND c.generation_id = d.active_generation_id"
                " ORDER BY c.ordinal",
                (ctx.tenant_id, kb_id, document_id),
            ).fetchall()
        return [str(row["content"]) for row in rows]

    def update_base_settings(
        self, ctx: WorkBuddyDbContext, kb_id: str, *, max_documents: int
    ) -> WorkBuddyKnowledgeBaseRow | None:
        """Replace the per-base document cap; ``None`` when the base is not visible."""
        cap = int(max_documents)
        if cap < 1 or cap > MAX_DOCUMENTS_PER_BASE:
            raise ValueError(f"max_documents must be between 1 and {MAX_DOCUMENTS_PER_BASE}")
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_bases SET max_documents = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND archived_at IS NULL "
                f"RETURNING {_BASE_COLUMNS}",
                (cap, now_ts(), ctx.tenant_id, kb_id),
            ).fetchone()
        return WorkBuddyKnowledgeBaseRow.from_row(row) if row is not None else None

    def document_cap_reached(
        self, ctx: WorkBuddyDbContext, kb_id: str, *, incoming: int = 1
    ) -> bool:
        """True when adding ``incoming`` documents would pass the base's cap.

        ``incoming=0`` answers "is the base already full"; the default answers
        "may one more be accepted", which is what the ingestion path asks.
        """
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT b.max_documents AS cap, ("
                " SELECT COUNT(*) FROM workbuddy_knowledge_documents d"
                " WHERE d.tenant_id = b.tenant_id AND d.kb_id = b.kb_id AND d.deleted_at IS NULL"
                ") AS held FROM workbuddy_knowledge_bases b"
                " WHERE b.tenant_id = ? AND b.kb_id = ? AND b.archived_at IS NULL",
                (ctx.tenant_id, kb_id),
            ).fetchone()
        if row is None:
            return False
        return int(row["held"]) + max(0, int(incoming)) > int(row["cap"])

    # ── per-member preferences ─────────────────────────────────────────────

    def set_default_open(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        user_id: int,
        default_open: bool,
    ) -> bool:
        """Open (or close) one base for one member; ``False`` when the base is invisible."""
        with workbuddy_transaction(self._db, ctx) as conn:
            base = conn.execute(
                "SELECT 1 FROM workbuddy_knowledge_bases"
                " WHERE tenant_id = ? AND kb_id = ? AND archived_at IS NULL",
                (ctx.tenant_id, kb_id),
            ).fetchone()
            if base is None:
                return False
            conn.execute(
                "INSERT INTO workbuddy_knowledge_preferences("
                " tenant_id, user_id, kb_id, default_open, updated_at"
                ") VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, user_id, kb_id) DO UPDATE SET"
                " default_open = EXCLUDED.default_open, updated_at = EXCLUDED.updated_at",
                (ctx.tenant_id, int(user_id), kb_id, bool(default_open), now_ts()),
            )
        return True

    def default_open_bases(self, ctx: WorkBuddyDbContext, *, user_id: int) -> dict[str, bool]:
        """``kb_id -> default_open`` for one member: absent means "not opened"."""
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT kb_id, default_open FROM workbuddy_knowledge_preferences"
                " WHERE tenant_id = ? AND user_id = ?",
                (ctx.tenant_id, int(user_id)),
            ).fetchall()
        return {str(row["kb_id"]): bool(row["default_open"]) for row in rows}

    def archive_base(
        self, ctx: WorkBuddyDbContext, kb_id: str, *, archived_by_user_id: int
    ) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_bases "
                "SET archived_at = ?, archived_by_user_id = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND archived_at IS NULL RETURNING kb_id",
                (now_ts(), archived_by_user_id, now_ts(), ctx.tenant_id, kb_id),
            ).fetchone()
        return row is not None

    # ── ACL ────────────────────────────────────────────────────────────────

    def add_acl(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        permission: str,
        granted_by_user_id: int,
        user_id: int | None = None,
        department_id: str | None = None,
    ) -> WorkBuddyKnowledgeAclRow:
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_knowledge_acl ("
                "tenant_id, acl_id, kb_id, user_id, department_id, permission, "
                "granted_by_user_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    new_uuid(),
                    kb_id,
                    user_id,
                    department_id,
                    permission,
                    granted_by_user_id,
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy knowledge ACL insert returned no row")
        return WorkBuddyKnowledgeAclRow.from_row(row)

    def list_acl(self, ctx: WorkBuddyDbContext, kb_id: str) -> list[WorkBuddyKnowledgeAclRow]:
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_knowledge_acl WHERE tenant_id = ? AND kb_id = ? "
                "ORDER BY created_at, acl_id",
                (ctx.tenant_id, kb_id),
            ).fetchall()
        return [WorkBuddyKnowledgeAclRow.from_row(row) for row in rows]

    def get_acl(
        self, ctx: WorkBuddyDbContext, kb_id: str, acl_id: str
    ) -> WorkBuddyKnowledgeAclRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_knowledge_acl "
                "WHERE tenant_id = ? AND kb_id = ? AND acl_id = ?",
                (ctx.tenant_id, kb_id, acl_id),
            ).fetchone()
        return WorkBuddyKnowledgeAclRow.from_row(row) if row is not None else None

    def update_acl_permission(
        self, ctx: WorkBuddyDbContext, kb_id: str, acl_id: str, *, permission: str
    ) -> WorkBuddyKnowledgeAclRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_acl SET permission = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND acl_id = ? RETURNING *",
                (permission, now_ts(), ctx.tenant_id, kb_id, acl_id),
            ).fetchone()
        return WorkBuddyKnowledgeAclRow.from_row(row) if row is not None else None

    def delete_acl(self, ctx: WorkBuddyDbContext, kb_id: str, acl_id: str) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "DELETE FROM workbuddy_knowledge_acl "
                "WHERE tenant_id = ? AND kb_id = ? AND acl_id = ? RETURNING acl_id",
                (ctx.tenant_id, kb_id, acl_id),
            ).fetchone()
        return row is not None

    def effective_acl(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        user_id: int,
        department_id: str | None,
    ) -> list[WorkBuddyKnowledgeAclRow]:
        """ACL rows that apply to the caller: their own or their department's.

        Read on every transaction, so deleting a row is effective immediately.
        """
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_knowledge_acl "
                "WHERE tenant_id = ? AND kb_id = ? AND (user_id = ? OR department_id = ?)",
                (ctx.tenant_id, kb_id, user_id, department_id),
            ).fetchall()
        return [WorkBuddyKnowledgeAclRow.from_row(row) for row in rows]

    def effective_acl_map(
        self,
        ctx: WorkBuddyDbContext,
        kb_ids: Sequence[str],
        *,
        user_id: int,
        department_id: str | None,
    ) -> dict[str, list[WorkBuddyKnowledgeAclRow]]:
        """Batched :meth:`effective_acl` for the knowledge base list view."""
        if not kb_ids:
            return {}
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_knowledge_acl "
                "WHERE tenant_id = ? AND kb_id = ANY(?) "
                "AND (user_id = ? OR department_id = ?)",
                (ctx.tenant_id, list(kb_ids), user_id, department_id),
            ).fetchall()
        grouped: dict[str, list[WorkBuddyKnowledgeAclRow]] = {}
        for row in rows:
            mapped = WorkBuddyKnowledgeAclRow.from_row(row)
            grouped.setdefault(mapped.kb_id, []).append(mapped)
        return grouped

    def department_exists(self, ctx: WorkBuddyDbContext, department_id: str) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM workbuddy_departments "
                "WHERE tenant_id = ? AND department_id = ? AND status = 'active'",
                (ctx.tenant_id, department_id),
            ).fetchone()
        return row is not None

    def tenant_member_exists(self, ctx: WorkBuddyDbContext, user_id: int) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM workbuddy_tenant_members "
                "WHERE tenant_id = ? AND user_id = ? AND status = 'active'",
                (ctx.tenant_id, user_id),
            ).fetchone()
        return row is not None

    # ── bound uploads and file references ──────────────────────────────────

    def create_upload(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        requested_by_user_id: int,
        filename: str,
        mime_type: str,
        size_bytes: int,
        object_key: str,
        ttl_seconds: int,
        upload_id: str | None = None,
    ) -> WorkBuddyKnowledgeUploadRow:
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_knowledge_uploads ("
                "tenant_id, upload_id, kb_id, requested_by_user_id, filename, mime_type, "
                "size_bytes, object_key, status, created_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    upload_id or new_uuid(),
                    kb_id,
                    requested_by_user_id,
                    filename,
                    mime_type,
                    size_bytes,
                    object_key,
                    stamp,
                    stamp + max(60, ttl_seconds),
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy knowledge upload insert returned no row")
        return WorkBuddyKnowledgeUploadRow.from_row(row)

    def get_upload(
        self, ctx: WorkBuddyDbContext, kb_id: str, upload_id: str
    ) -> WorkBuddyKnowledgeUploadRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_knowledge_uploads "
                "WHERE tenant_id = ? AND kb_id = ? AND upload_id = ?",
                (ctx.tenant_id, kb_id, upload_id),
            ).fetchone()
        return WorkBuddyKnowledgeUploadRow.from_row(row) if row is not None else None

    def reject_upload(
        self, ctx: WorkBuddyDbContext, kb_id: str, upload_id: str, *, rejection_code: str
    ) -> None:
        with workbuddy_transaction(self._db, ctx) as conn:
            conn.execute(
                "UPDATE workbuddy_knowledge_uploads "
                "SET status = 'rejected', rejection_code = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND upload_id = ? AND status = 'pending'",
                (rejection_code, ctx.tenant_id, kb_id, upload_id),
            )

    def complete_upload(
        self,
        ctx: WorkBuddyDbContext,
        upload: WorkBuddyKnowledgeUploadRow,
        *,
        checksum_sha256: str,
        detected_mime: str,
        scan_status: str,
        completed_by_user_id: int,
        file_ref_id: str | None = None,
    ) -> WorkBuddyKnowledgeFileRefRow:
        """Create the bound file reference and close the upload in one transaction."""
        stamp = now_ts()
        public_id = file_ref_id or new_uuid()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_knowledge_file_refs ("
                "tenant_id, file_ref_id, kb_id, upload_id, object_key, filename, mime_type, "
                "size_bytes, checksum_sha256, created_by_user_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    public_id,
                    upload.kb_id,
                    upload.upload_id,
                    upload.object_key,
                    upload.filename,
                    upload.mime_type,
                    upload.size_bytes,
                    checksum_sha256,
                    completed_by_user_id,
                    stamp,
                ),
            ).fetchone()
            conn.execute(
                "UPDATE workbuddy_knowledge_uploads "
                "SET status = 'completed', checksum_sha256 = ?, detected_mime = ?, scan_status = ?, "
                "file_ref_id = ?, completed_at = ? "
                "WHERE tenant_id = ? AND upload_id = ? AND status = 'pending'",
                (
                    checksum_sha256,
                    detected_mime,
                    scan_status,
                    public_id,
                    stamp,
                    ctx.tenant_id,
                    upload.upload_id,
                ),
            )
        if row is None:
            raise RuntimeError("workbuddy knowledge file reference insert returned no row")
        return WorkBuddyKnowledgeFileRefRow.from_row(row)

    def get_file_ref(
        self, ctx: WorkBuddyDbContext, kb_id: str, file_ref_id: str
    ) -> WorkBuddyKnowledgeFileRefRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_knowledge_file_refs "
                "WHERE tenant_id = ? AND kb_id = ? AND file_ref_id = ?",
                (ctx.tenant_id, kb_id, file_ref_id),
            ).fetchone()
        return WorkBuddyKnowledgeFileRefRow.from_row(row) if row is not None else None

    # ── documents ──────────────────────────────────────────────────────────

    def create_document(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        file_ref_id: str | None,
        title: str,
        created_by_user_id: int,
        document_id: str | None = None,
        job_id: str | None = None,
        source: str = "upload",
    ) -> WorkBuddyKnowledgeDocumentRow:
        """Insert one document; ``file_ref_id`` is ``None`` for text-only sources."""
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_knowledge_documents ("
                "tenant_id, document_id, kb_id, file_ref_id, source, title, status, job_id, "
                "created_by_user_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    document_id or new_uuid(),
                    kb_id,
                    file_ref_id,
                    source,
                    title,
                    job_id or new_uuid(),
                    created_by_user_id,
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy knowledge document insert returned no row")
        return WorkBuddyKnowledgeDocumentRow.from_row(row)

    def get_document(
        self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str
    ) -> WorkBuddyKnowledgeDocumentRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_knowledge_documents "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ?",
                (ctx.tenant_id, kb_id, document_id),
            ).fetchone()
        return WorkBuddyKnowledgeDocumentRow.from_row(row) if row is not None else None

    def list_documents(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        limit: int = MAX_LIST_DOCUMENTS,
        include_deleted: bool = False,
    ) -> list[WorkBuddyKnowledgeDocumentRow]:
        clause = "" if include_deleted else " AND deleted_at IS NULL"
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_knowledge_documents "
                f"WHERE tenant_id = ? AND kb_id = ?{clause} "
                "ORDER BY created_at DESC, document_id LIMIT ?",
                (ctx.tenant_id, kb_id, max(1, min(limit, MAX_LIST_DOCUMENTS))),
            ).fetchall()
        return [WorkBuddyKnowledgeDocumentRow.from_row(row) for row in rows]

    def set_document_status(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        document_id: str,
        *,
        status: str,
        error_code: str | None = None,
    ) -> None:
        with workbuddy_transaction(self._db, ctx) as conn:
            conn.execute(
                "UPDATE workbuddy_knowledge_documents "
                "SET status = ?, error_code = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ? AND deleted_at IS NULL",
                (status, error_code, now_ts(), ctx.tenant_id, kb_id, document_id),
            )

    def soft_delete_document(self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str) -> bool:
        """Remove retrievability immediately; physical rows stay for the reaper."""
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_knowledge_documents "
                "SET status = 'deleted', active_generation_id = NULL, deleted_at = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ? AND deleted_at IS NULL "
                "RETURNING document_id",
                (stamp, stamp, ctx.tenant_id, kb_id, document_id),
            ).fetchone()
        return row is not None

    def next_generation_number(self, ctx: WorkBuddyDbContext, kb_id: str, document_id: str) -> int:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT coalesce(max(generation_number), 0) AS current "
                "FROM workbuddy_knowledge_generations "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ?",
                (ctx.tenant_id, kb_id, document_id),
            ).fetchone()
        return int(row["current"]) + 1 if row is not None else 1

    def publish_generation(
        self,
        ctx: WorkBuddyDbContext,
        *,
        base: WorkBuddyKnowledgeBaseRow,
        document_id: str,
        created_by_user_id: int,
        chunks: list[tuple[int, str, int, dict[str, Any], list[float]]],
        generation_id: str | None = None,
    ) -> WorkBuddyKnowledgeGenerationRow:
        """Publish a ready generation and its chunks in one transaction.

        The generation row, every chunk and the document's ``active_generation_id``
        pointer land together, so an unfinished index is never visible to search.
        """
        public_id = generation_id or new_uuid()
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT coalesce(max(generation_number), 0) AS current "
                "FROM workbuddy_knowledge_generations "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ?",
                (ctx.tenant_id, base.kb_id, document_id),
            ).fetchone()
            number = (int(row["current"]) + 1) if row is not None else 1
            generation = conn.execute(
                "INSERT INTO workbuddy_knowledge_generations ("
                "tenant_id, generation_id, kb_id, document_id, generation_number, status, "
                "embedding_model_revision_id, embedding_adapter_key, embedding_model_key, "
                "embedding_revision, embedding_dimensions, chunk_count, created_by_user_id, "
                "created_at, ready_at"
                ") VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    public_id,
                    base.kb_id,
                    document_id,
                    number,
                    base.embedding_model_revision_id,
                    base.embedding_adapter_key,
                    base.embedding_model_key,
                    base.embedding_revision,
                    base.embedding_dimensions,
                    len(chunks),
                    created_by_user_id,
                    stamp,
                    stamp,
                ),
            ).fetchone()
            for ordinal, content, token_count, metadata, embedding in chunks:
                conn.execute(
                    "INSERT INTO workbuddy_knowledge_chunks ("
                    "tenant_id, chunk_id, kb_id, document_id, generation_id, ordinal, content, "
                    "token_count, metadata, embedding, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::vector, ?)",
                    (
                        ctx.tenant_id,
                        new_uuid(),
                        base.kb_id,
                        document_id,
                        public_id,
                        ordinal,
                        content,
                        token_count,
                        json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                        vector_literal(embedding),
                        stamp,
                    ),
                )
            conn.execute(
                "UPDATE workbuddy_knowledge_documents "
                "SET status = 'ready', error_code = NULL, active_generation_id = ?, "
                "chunk_count = ?, updated_at = ? "
                "WHERE tenant_id = ? AND kb_id = ? AND document_id = ? AND deleted_at IS NULL",
                (public_id, len(chunks), stamp, ctx.tenant_id, base.kb_id, document_id),
            )
        if generation is None:
            raise RuntimeError("workbuddy knowledge generation insert returned no row")
        return WorkBuddyKnowledgeGenerationRow.from_row(generation)

    def get_generation(
        self, ctx: WorkBuddyDbContext, kb_id: str, generation_id: str
    ) -> WorkBuddyKnowledgeGenerationRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_knowledge_generations "
                "WHERE tenant_id = ? AND kb_id = ? AND generation_id = ?",
                (ctx.tenant_id, kb_id, generation_id),
            ).fetchone()
        return WorkBuddyKnowledgeGenerationRow.from_row(row) if row is not None else None

    def search_chunks(
        self,
        ctx: WorkBuddyDbContext,
        kb_id: str,
        *,
        query_vector: list[float],
        limit: int,
    ) -> list[WorkBuddyKnowledgeChunkHit]:
        """Cosine search inside one base, one ready generation, one tenant."""
        literal = vector_literal(query_vector)
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT c.chunk_id, c.document_id, d.title AS document_title, c.generation_id, "
                "c.ordinal, c.content, c.token_count, c.metadata, "
                "1 - (c.embedding <=> ?::vector) AS score "
                "FROM workbuddy_knowledge_chunks c "
                "JOIN workbuddy_knowledge_documents d "
                "  ON d.tenant_id = c.tenant_id AND d.document_id = c.document_id "
                "JOIN workbuddy_knowledge_generations g "
                "  ON g.tenant_id = c.tenant_id AND g.generation_id = c.generation_id "
                "WHERE c.tenant_id = ? AND c.kb_id = ? "
                "  AND d.deleted_at IS NULL AND d.status = 'ready' "
                "  AND g.status = 'ready' AND d.active_generation_id = c.generation_id "
                "ORDER BY c.embedding <=> ?::vector "
                "LIMIT ?",
                (literal, ctx.tenant_id, kb_id, literal, max(1, min(limit, 50))),
            ).fetchall()
        return [WorkBuddyKnowledgeChunkHit.from_row(row) for row in rows]


def _registration_columns() -> str:
    return (
        "registration_id, tenant_id, workflow_id, kind, name, enabled, webhook_path, "
        "cron_expression, event_name, event_filter, secret_provider, secret_ref, secret_version, "
        "previous_secret_ref, previous_secret_expires_at, signature_algorithm, signature_header, "
        "timestamp_header, tolerance_seconds, created_by_user_id, created_at, updated_at, revoked_at"
    )


class WorkBuddyTriggerRepo:
    """SQL for trigger registrations, capability grants and the event-key ledger."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create_registration(
        self,
        ctx: WorkBuddyDbContext,
        *,
        workflow_id: str,
        kind: str,
        name: str,
        created_by_user_id: int,
        webhook_path: str | None = None,
        cron_expression: str | None = None,
        event_name: str | None = None,
        event_filter: dict[str, Any] | None = None,
        secret_provider: str | None = None,
        secret_ref: str | None = None,
        secret_version: int = 0,
        signature_header: str = "x-workbuddy-signature",
        timestamp_header: str = "x-workbuddy-timestamp",
        tolerance_seconds: int = 300,
        registration_id: str | None = None,
    ) -> WorkBuddyTriggerRegistrationRow:
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_trigger_registrations ("
                "tenant_id, registration_id, workflow_id, kind, name, enabled, webhook_path, "
                "cron_expression, event_name, event_filter, secret_provider, secret_ref, "
                "secret_version, signature_header, timestamp_header, tolerance_seconds, "
                "created_by_user_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?::jsonb, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"RETURNING {_registration_columns()}",
                (
                    ctx.tenant_id,
                    registration_id or new_uuid(),
                    workflow_id,
                    kind,
                    name,
                    webhook_path,
                    cron_expression,
                    event_name,
                    json.dumps(event_filter or {}, separators=(",", ":"), sort_keys=True),
                    secret_provider,
                    secret_ref,
                    secret_version,
                    signature_header,
                    timestamp_header,
                    tolerance_seconds,
                    created_by_user_id,
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy trigger registration insert returned no row")
        return WorkBuddyTriggerRegistrationRow.from_row(row)

    def get_registration(
        self, ctx: WorkBuddyDbContext, registration_id: str
    ) -> WorkBuddyTriggerRegistrationRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"SELECT {_registration_columns()} FROM workbuddy_trigger_registrations "
                "WHERE tenant_id = ? AND registration_id = ?",
                (ctx.tenant_id, registration_id),
            ).fetchone()
        return WorkBuddyTriggerRegistrationRow.from_row(row) if row is not None else None

    def list_registrations(
        self, ctx: WorkBuddyDbContext, workflow_id: str
    ) -> list[WorkBuddyTriggerRegistrationRow]:
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                f"SELECT {_registration_columns()} FROM workbuddy_trigger_registrations "
                "WHERE tenant_id = ? AND workflow_id = ? ORDER BY created_at, registration_id",
                (ctx.tenant_id, workflow_id),
            ).fetchall()
        return [WorkBuddyTriggerRegistrationRow.from_row(row) for row in rows]

    def workflow_exists(self, ctx: WorkBuddyDbContext, workflow_id: str) -> bool:
        """Read-only existence check against the workflow slice's table (017)."""
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM workbuddy_workflows "
                "WHERE tenant_id = ? AND workflow_id = ?",
                (ctx.tenant_id, workflow_id),
            ).fetchone()
        return row is not None

    def resolve_webhook_registration(
        self, webhook_path: str
    ) -> WorkBuddyTriggerRegistrationRow | None:
        """Resolve a public webhook path before any tenant is known.

        Runs under the platform context: the path is globally unique, so at most
        one registration row is returned and the caller immediately re-enters the
        row's own tenant context for every further statement.
        """
        with workbuddy_transaction(self._db, WorkBuddyDbContext.platform()) as conn:
            row = conn.execute(
                f"SELECT {_registration_columns()} FROM workbuddy_trigger_registrations "
                "WHERE webhook_path = ?",
                (webhook_path,),
            ).fetchone()
        return WorkBuddyTriggerRegistrationRow.from_row(row) if row is not None else None

    def revoke_registration(self, ctx: WorkBuddyDbContext, registration_id: str) -> bool:
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_trigger_registrations "
                "SET enabled = FALSE, revoked_at = ?, updated_at = ? "
                "WHERE tenant_id = ? AND registration_id = ? AND revoked_at IS NULL "
                "RETURNING registration_id",
                (stamp, stamp, ctx.tenant_id, registration_id),
            ).fetchone()
        return row is not None

    def update_registration_secret(
        self,
        ctx: WorkBuddyDbContext,
        registration_id: str,
        *,
        secret_provider: str,
        secret_ref: str,
        secret_version: int,
        previous_secret_ref: str | None,
        previous_secret_expires_at: int | None,
    ) -> WorkBuddyTriggerRegistrationRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_trigger_registrations "
                "SET secret_provider = ?, secret_ref = ?, secret_version = ?, "
                "previous_secret_ref = ?, previous_secret_expires_at = ?, updated_at = ? "
                "WHERE tenant_id = ? AND registration_id = ? AND revoked_at IS NULL "
                f"RETURNING {_registration_columns()}",
                (
                    secret_provider,
                    secret_ref,
                    secret_version,
                    previous_secret_ref,
                    previous_secret_expires_at,
                    now_ts(),
                    ctx.tenant_id,
                    registration_id,
                ),
            ).fetchone()
        return WorkBuddyTriggerRegistrationRow.from_row(row) if row is not None else None

    # ── capability grants ──────────────────────────────────────────────────

    def add_grant(
        self,
        ctx: WorkBuddyDbContext,
        registration_id: str,
        *,
        created_by_user_id: int,
        tool_name: str | None = None,
        kb_id: str | None = None,
        kb_permission: str | None = None,
    ) -> WorkBuddyTriggerGrantRow:
        stamp = now_ts()
        capability = "tool" if tool_name is not None else "knowledge_base"
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "INSERT INTO workbuddy_trigger_grants ("
                "tenant_id, grant_id, registration_id, capability, tool_name, kb_id, permission, "
                "created_by_user_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (
                    ctx.tenant_id,
                    new_uuid(),
                    registration_id,
                    capability,
                    tool_name,
                    kb_id,
                    kb_permission,
                    created_by_user_id,
                    stamp,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("workbuddy trigger grant insert returned no row")
        return WorkBuddyTriggerGrantRow.from_row(row)

    def list_grants(
        self, ctx: WorkBuddyDbContext, registration_id: str
    ) -> list[WorkBuddyTriggerGrantRow]:
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_trigger_grants "
                "WHERE tenant_id = ? AND registration_id = ? ORDER BY created_at, grant_id",
                (ctx.tenant_id, registration_id),
            ).fetchall()
        return [WorkBuddyTriggerGrantRow.from_row(row) for row in rows]

    # ── persistent event-key ledger ────────────────────────────────────────

    def claim_delivery(
        self,
        ctx: WorkBuddyDbContext,
        registration_id: str,
        *,
        event_key: str,
        body_sha256: str,
        event_name: str | None,
        signature_version: int | None,
        signature_timestamp: int | None,
        is_test: bool,
        actor_user_id: int | None,
    ) -> DeliveryClaim:
        """Claim an event key exactly once; retry only after a failed attempt."""
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            inserted = conn.execute(
                "INSERT INTO workbuddy_trigger_deliveries ("
                "tenant_id, delivery_id, registration_id, event_key, event_name, status, is_test, "
                "body_sha256, signature_version, signature_timestamp, attempt, actor_user_id, received_at"
                ") VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT (tenant_id, registration_id, event_key) DO NOTHING RETURNING *",
                (
                    ctx.tenant_id,
                    new_uuid(),
                    registration_id,
                    event_key,
                    event_name,
                    is_test,
                    body_sha256,
                    signature_version,
                    signature_timestamp,
                    actor_user_id,
                    stamp,
                ),
            ).fetchone()
            if inserted is not None:
                return DeliveryClaim(WorkBuddyTriggerDeliveryRow.from_row(inserted), True)
            existing = conn.execute(
                "SELECT * FROM workbuddy_trigger_deliveries "
                "WHERE tenant_id = ? AND registration_id = ? AND event_key = ?",
                (ctx.tenant_id, registration_id, event_key),
            ).fetchone()
            if existing is None:
                raise RuntimeError("workbuddy trigger delivery claim lost its row")
            status = str(existing["status"])
            if status != "failed":
                return DeliveryClaim(WorkBuddyTriggerDeliveryRow.from_row(existing), False)
            reclaimed = conn.execute(
                "UPDATE workbuddy_trigger_deliveries "
                "SET status = 'accepted', attempt = attempt + 1, body_sha256 = ?, "
                "signature_timestamp = ?, received_at = ?, completed_at = NULL "
                "WHERE tenant_id = ? AND registration_id = ? AND event_key = ? AND status = 'failed' "
                "RETURNING *",
                (
                    body_sha256,
                    signature_timestamp,
                    stamp,
                    ctx.tenant_id,
                    registration_id,
                    event_key,
                ),
            ).fetchone()
            if reclaimed is None:
                return DeliveryClaim(WorkBuddyTriggerDeliveryRow.from_row(existing), False)
            return DeliveryClaim(WorkBuddyTriggerDeliveryRow.from_row(reclaimed), True)

    def complete_delivery(
        self,
        ctx: WorkBuddyDbContext,
        delivery_id: str,
        *,
        execution_id: str,
    ) -> None:
        with workbuddy_transaction(self._db, ctx) as conn:
            conn.execute(
                "UPDATE workbuddy_trigger_deliveries "
                "SET status = 'executed', execution_id = ?, completed_at = ? "
                "WHERE tenant_id = ? AND delivery_id = ? AND status = 'accepted'",
                (execution_id, now_ts(), ctx.tenant_id, delivery_id),
            )

    def reject_delivery(
        self,
        ctx: WorkBuddyDbContext,
        delivery_id: str,
        *,
        rejection_code: str,
    ) -> None:
        with workbuddy_transaction(self._db, ctx) as conn:
            conn.execute(
                "UPDATE workbuddy_trigger_deliveries "
                "SET status = 'failed', rejection_code = ?, completed_at = ? "
                "WHERE tenant_id = ? AND delivery_id = ? AND status = 'accepted'",
                (rejection_code, now_ts(), ctx.tenant_id, delivery_id),
            )

    def get_delivery(
        self, ctx: WorkBuddyDbContext, delivery_id: str
    ) -> WorkBuddyTriggerDeliveryRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT * FROM workbuddy_trigger_deliveries "
                "WHERE tenant_id = ? AND delivery_id = ?",
                (ctx.tenant_id, delivery_id),
            ).fetchone()
        return WorkBuddyTriggerDeliveryRow.from_row(row) if row is not None else None

    def list_deliveries(
        self,
        ctx: WorkBuddyDbContext,
        registration_id: str,
        *,
        limit: int = MAX_LIST_DELIVERIES,
    ) -> list[WorkBuddyTriggerDeliveryRow]:
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                "SELECT * FROM workbuddy_trigger_deliveries "
                "WHERE tenant_id = ? AND registration_id = ? "
                "ORDER BY received_at DESC, delivery_id LIMIT ?",
                (ctx.tenant_id, registration_id, max(1, min(limit, MAX_LIST_DELIVERIES))),
            ).fetchall()
        return [WorkBuddyTriggerDeliveryRow.from_row(row) for row in rows]

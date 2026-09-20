"""Knowledge retrieval and trigger services for WorkBuddy (fail closed).

This module owns the two business rules that must never be approximated:

**Knowledge access precedence** (:func:`resolve_knowledge_access`)

* ``enterprise`` base  — every active tenant member may read
* ``department`` base  — only members currently in that department may read
* ``personal`` base    — only the owner; a tenant admin has *no* implicit read
* explicit ACL rows    — additive, read on every transaction, so revoking one
  takes effect on the next statement
* archived base        — invisible to everyone, immediately

**The pinned embedding revision** — a base pins exactly one platform model
revision, and only two shapes can carry it:

* ``bge-m3`` — the hosted revision the platform constant describes;
* a local ONNX revision — ``adapter_key='onnx'`` that declares both its width
  and the downloaded local model id, at the storage layer's width. It reaches a
  tenant through the same platform grant path as any other revision (publish is
  an immutable new row, revocation only flips status);
* anything else — refused with ``MODEL_NOT_CONFIGURED``, the code every existing
  unpinnable model already gets.

**Fail-closed dependencies** — object store, content scanner, document parser,
embedder and the external secret backend are injected hooks. When a hook is
missing, unproven or reports a mismatch, the caller gets ``DEPENDENCY_UNAVAILABLE``
or ``MODEL_NOT_CONFIGURED`` and nothing is published: no upload is completed, no
generation becomes ready, no webhook is dispatched, and no signing secret is
rotated. Hook output is never trusted blindly: upload bytes are re-measured and
re-hashed, archive manifests are re-validated for path traversal and zip bombs,
and every embedding vector is re-checked for width, finiteness and non-zero
norm before it can reach pgvector.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_catalog import ADAPTER_KEY_ONNX
from octop.infra.db.repos.workbuddy_knowledge import (
    EMBEDDING_DIMENSIONS,
    MAX_LIST_BASES,
    DeliveryClaim,
    WorkBuddyKnowledgeAclRow,
    WorkBuddyKnowledgeBaseRow,
    WorkBuddyKnowledgeDocumentRow,
    WorkBuddyKnowledgeFileRefRow,
    WorkBuddyKnowledgeRepo,
    WorkBuddyPlatformModelRevisionRow,
    WorkBuddyTriggerDeliveryRow,
    WorkBuddyTriggerRegistrationRow,
    WorkBuddyTriggerRepo,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.log_redaction import register_secret
from octop.infra.workbuddy.runtime import RuntimeJobRecorder

#: The only retrieval model WorkBuddy pins into a knowledge base.
BGE_M3_MODEL_KEY = "bge-m3"
#: pgvector width of every stored and queried embedding.
VECTOR_DIMENSIONS = EMBEDDING_DIMENSIONS

MAX_QUERY_LENGTH = 2_000
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
MAX_MATCH_COUNT = 50
UPLOAD_TTL_SECONDS = 900

#: Document sources.  ``upload`` names a stored file reference; the other two are
#: text-only: ``text`` for content a caller supplies, ``migration`` for content
#: imported from the personal edition (chunk text and vectors, no original file).
DOCUMENT_SOURCE_UPLOAD = "upload"
DOCUMENT_SOURCE_TEXT = "text"
DOCUMENT_SOURCE_MIGRATION = "migration"
DOCUMENT_SOURCES = (DOCUMENT_SOURCE_UPLOAD, DOCUMENT_SOURCE_TEXT, DOCUMENT_SOURCE_MIGRATION)
TEXT_DOCUMENT_SOURCES = (DOCUMENT_SOURCE_TEXT, DOCUMENT_SOURCE_MIGRATION)
#: Same bound as the ``wb_knowledge_documents_title_length`` row constraint.
DOCUMENT_TITLE_MAX_LENGTH = 255
#: Fixed overlap window during which the previous webhook secret still verifies.
SECRET_OVERLAP_SECONDS = 300
DEFAULT_TOLERANCE_SECONDS = 300
DEFAULT_EVENT_KEY_HEADER = "x-workbuddy-event-id"

PERMISSION_RANK = {"read": 1, "write": 2, "admin": 3}
KNOWLEDGE_SCOPES = ("personal", "department", "enterprise")

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_FILENAME = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f]{1,255}$")
_WEBHOOK_PATH = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_EXTENSION_MIME: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_TEXT_MIMES = {"text/plain", "text/markdown", "text/csv", "application/json"}
_ZIP_MIMES = {
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_MIME_HEAD_OFFSETS = 4096


class KnowledgeHookUnavailable(RuntimeError):
    """A required hook is not configured or not proven (fail closed)."""


class HookRejection(ValueError):
    """Hook output or upload content failed a bounded validation check."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


# ── hooks ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ObjectStoreTarget:
    object_key: str
    upload_url: str | None = None


@dataclass(frozen=True, slots=True)
class ObjectStat:
    size_bytes: int
    checksum_sha256: str | None = None


@runtime_checkable
class ObjectStoreHook(Protocol):
    """Bounded object storage; never returns more bytes than requested."""

    def available(self) -> bool: ...

    def allocate_target(
        self,
        *,
        tenant_id: str,
        kb_id: str,
        upload_id: str,
        filename: str,
    ) -> ObjectStoreTarget: ...

    def stat(self, object_key: str) -> ObjectStat | None: ...

    def read(self, object_key: str, *, limit: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    status: str  # 'clean' | 'infected' | 'unsupported'
    detail: str = ""


@runtime_checkable
class ContentScannerHook(Protocol):
    def available(self) -> bool: ...

    def scan(self, data: bytes, *, filename: str, mime_type: str) -> ScanVerdict: ...


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One member of an archive manifest, as reported by a parser hook."""

    name: str
    uncompressed_bytes: int
    compressed_bytes: int = 0
    is_directory: bool = False


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text_blocks: tuple[TextBlock, ...] = ()
    archive_entries: tuple[ArchiveEntry, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.text_blocks)


@runtime_checkable
class DocumentParserHook(Protocol):
    def available(self) -> bool: ...

    def parse(self, data: bytes, *, filename: str, mime_type: str) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class EmbeddingDescriptor:
    adapter_key: str
    model_key: str
    revision: int
    dimensions: int = VECTOR_DIMENSIONS


@runtime_checkable
class EmbeddingHook(Protocol):
    def available(self) -> bool: ...

    def describe(self) -> EmbeddingDescriptor | None: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class SecretBackendHook(Protocol):
    """External secret storage; WorkBuddy stores only the returned reference."""

    def available(self) -> bool: ...

    def create_secret(self, *, name: str, value: str) -> str: ...

    def read_secret(self, reference: str) -> str | None: ...

    def delete_secret(self, reference: str) -> None: ...


@runtime_checkable
class ExecutionDispatchHook(Protocol):
    """Hands an accepted trigger event to the workflow runtime (idempotent)."""

    def available(self) -> bool: ...

    def dispatch(
        self,
        *,
        tenant_id: str,
        registration: WorkBuddyTriggerRegistrationRow,
        delivery_id: str,
        event_key: str,
        event: Mapping[str, Any],
        is_test: bool,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class KnowledgeHooks:
    object_store: ObjectStoreHook | None = None
    scanner: ContentScannerHook | None = None
    parser: DocumentParserHook | None = None
    embedder: EmbeddingHook | None = None


@dataclass(frozen=True, slots=True)
class TriggerHooks:
    secret_backend: SecretBackendHook | None = None
    dispatcher: ExecutionDispatchHook | None = None


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Bounded archive inspection limits (zip-bomb guard)."""

    max_entries: int = 512
    max_entry_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_ratio: float = 100.0
    ratio_floor_bytes: int = 1024 * 1024
    max_name_length: int = 255
    max_depth: int = 8


_KNOWLEDGE_HOOKS = KnowledgeHooks()
_TRIGGER_HOOKS = TriggerHooks()


def configure_workbuddy_knowledge_hooks(hooks: KnowledgeHooks) -> None:
    """Install the deployment's knowledge hooks (object store, scanner, parser, embedder)."""
    global _KNOWLEDGE_HOOKS
    _KNOWLEDGE_HOOKS = hooks


def current_workbuddy_knowledge_hooks() -> KnowledgeHooks:
    return _KNOWLEDGE_HOOKS


def reset_workbuddy_knowledge_hooks() -> None:
    global _KNOWLEDGE_HOOKS
    _KNOWLEDGE_HOOKS = KnowledgeHooks()


def configure_workbuddy_trigger_hooks(hooks: TriggerHooks) -> None:
    """Install the deployment's trigger hooks (secret backend, dispatcher)."""
    global _TRIGGER_HOOKS
    _TRIGGER_HOOKS = hooks


def current_workbuddy_trigger_hooks() -> TriggerHooks:
    return _TRIGGER_HOOKS


def reset_workbuddy_trigger_hooks() -> None:
    global _TRIGGER_HOOKS
    _TRIGGER_HOOKS = TriggerHooks()


def _hook_available(hook: Any) -> bool:
    if hook is None or not hasattr(hook, "available"):
        return False
    try:
        return bool(hook.available())
    except Exception:  # a probe that raises is not proof of availability
        return False


def require_object_store(hook: ObjectStoreHook | None) -> ObjectStoreHook:
    if not _hook_available(hook):
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "object storage is not configured or not proven",
        )
    return hook  # type: ignore[return-value]


def require_scanner(hook: ContentScannerHook | None) -> ContentScannerHook:
    if not _hook_available(hook):
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "content scanner is not configured or not proven",
        )
    return hook  # type: ignore[return-value]


def require_parser(hook: DocumentParserHook | None) -> DocumentParserHook:
    if not _hook_available(hook):
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "document parser is not configured or not proven",
        )
    return hook  # type: ignore[return-value]


def require_embedder(hook: EmbeddingHook | None) -> EmbeddingHook:
    if not _hook_available(hook):
        raise OctopError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"embedding model {BGE_M3_MODEL_KEY} is not configured or not proven",
        )
    return hook  # type: ignore[return-value]


def _pinnable_local_model(revision: WorkBuddyPlatformModelRevisionRow) -> bool:
    """True when a local ONNX model revision may back a knowledge base (B-11).

    Three conditions, all required: the adapter is ``onnx``, the revision names
    the downloaded local model, and the width it declares is the storage layer's
    width. The pinned base stores the platform constant and every vector column
    is that fixed width, so a revision declaring another width could be
    published but never embedded into a base; it is refused with the same
    ``MODEL_NOT_CONFIGURED`` an unpinnable model already gets.
    """
    return (
        revision.adapter_key == ADAPTER_KEY_ONNX
        and bool(revision.local_model_id)
        and revision.embedding_dimensions == VECTOR_DIMENSIONS
    )


# ── pure validators (upload, archive, query, signature) ──────────────────────


def sanitize_filename(filename: str) -> str:
    """Return a bare file name, rejecting traversal and control characters."""
    name = (filename or "").strip()
    if not name:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "filename is required")
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise HookRejection("PATH_TRAVERSAL", "filename must not contain a path")
    if name != name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
        raise HookRejection("PATH_TRAVERSAL", "filename must be a bare name")
    if not _FILENAME.fullmatch(name):
        raise HookRejection("PATH_TRAVERSAL", "filename contains forbidden characters")
    if name.startswith("~"):
        raise HookRejection("PATH_TRAVERSAL", "filename must not be a home-relative path")
    return name


def file_extension(filename: str) -> str:
    _, dot, suffix = filename.rpartition(".")
    return f".{suffix.lower()}" if dot else ""


def canonical_mime(filename: str) -> str | None:
    return _EXTENSION_MIME.get(file_extension(filename))


def _magic_matches(mime_type: str, head: bytes) -> bool:
    if mime_type in _TEXT_MIMES:
        return b"\x00" not in head
    if mime_type == "application/pdf":
        return head.startswith(b"%PDF-")
    if mime_type in _ZIP_MIMES:
        return head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06")
    if mime_type == "image/png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return head.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    return False


def verify_upload_content(
    *,
    filename: str,
    declared_mime: str,
    data: bytes,
    declared_size: int | None = None,
) -> str:
    """Validate size, declared MIME type and magic bytes; return the canonical type."""
    expected = canonical_mime(filename)
    if expected is None:
        raise OctopError(
            ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
            "file type is not supported for knowledge indexing",
        )
    if declared_mime != expected:
        raise OctopError(
            ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
            "declared media type does not match the file extension",
        )
    if declared_size is not None and len(data) != declared_size:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "uploaded object size does not match the declared size",
        )
    if not data:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "uploaded object is empty")
    if not _magic_matches(expected, data[:_MIME_HEAD_OFFSETS]):
        raise OctopError(
            ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
            "file content does not match its declared media type",
        )
    return expected


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_archive_manifest(
    entries: Sequence[ArchiveEntry], *, limits: ArchiveLimits | None = None
) -> None:
    """Reject zip bombs and path traversal in a parser-reported archive manifest."""
    rules = limits or ArchiveLimits()
    if len(entries) > rules.max_entries:
        raise HookRejection("ARCHIVE_ENTRY_LIMIT", "archive contains too many entries")
    total = 0
    for entry in entries:
        name = entry.name or ""
        if entry.is_directory:
            continue
        if len(name) > rules.max_name_length:
            raise HookRejection("ARCHIVE_PATH_TRAVERSAL", "archive member name is too long")
        if name.startswith(("/", "\\")) or "\\" in name or re.match(r"^[A-Za-z]:", name):
            raise HookRejection("ARCHIVE_PATH_TRAVERSAL", "archive member uses an absolute path")
        parts = [part for part in name.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise HookRejection("ARCHIVE_PATH_TRAVERSAL", "archive member escapes the archive root")
        if len(parts) > rules.max_depth:
            raise HookRejection("ARCHIVE_PATH_TRAVERSAL", "archive member is nested too deeply")
        if not _FILENAME.fullmatch(parts[-1] if parts else name):
            raise HookRejection("ARCHIVE_PATH_TRAVERSAL", "archive member name is invalid")
        if entry.uncompressed_bytes < 0 or entry.compressed_bytes < 0:
            raise HookRejection("ARCHIVE_BOMB", "archive member reports a negative size")
        total += entry.uncompressed_bytes
        if entry.uncompressed_bytes > rules.max_entry_bytes:
            raise HookRejection("ARCHIVE_BOMB", "archive member expands beyond the limit")
        ratio_base = max(entry.compressed_bytes, 1)
        ratio = entry.uncompressed_bytes / ratio_base
        if ratio > rules.max_ratio and entry.uncompressed_bytes > rules.ratio_floor_bytes:
            raise HookRejection("ARCHIVE_BOMB", "archive member compression ratio is unsafe")
    if total > rules.max_total_bytes:
        raise HookRejection("ARCHIVE_BOMB", "archive expands beyond the total limit")


def validate_query_text(query: str) -> str:
    text = (query or "").strip()
    if not text:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "query is required")
    if len(text) > MAX_QUERY_LENGTH:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "query is too long")
    return text


def validate_embedding_vector(
    vector: Sequence[float] | None, *, dimensions: int = VECTOR_DIMENSIONS
) -> list[float]:
    """Reject a vector that is not exactly ``dimensions`` wide, finite and non-zero."""
    if vector is None or not isinstance(vector, (list, tuple)) or len(vector) != dimensions:
        raise OctopError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            f"embedding vector must have exactly {dimensions} dimensions",
        )
    values: list[float] = []
    for raw in vector:
        if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "embedding vector contains a non-finite value",
            )
        values.append(float(raw))
    if not any(value != 0.0 for value in values):
        raise OctopError(
            ErrorCode.MODEL_NOT_CONFIGURED,
            "embedding vector must not be all zeros",
        )
    return values


def compute_webhook_signature(*, secret: str, timestamp: int, body: bytes) -> str:
    """HMAC-SHA256 over ``timestamp.body`` (raw request body, never re-serialized)."""
    material = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def normalize_signature(value: str | None) -> str:
    text = (value or "").strip().lower()
    for prefix in ("sha256=", "v1="):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text


def timestamp_in_window(timestamp: int, *, tolerance_seconds: int, now: int) -> bool:
    return abs(now - timestamp) <= max(1, tolerance_seconds)


def signature_matches(*, secret: str, timestamp: int, body: bytes, provided: str | None) -> bool:
    candidate = normalize_signature(provided)
    if not candidate or not _SHA256_HEX.fullmatch(candidate):
        return False
    return hmac.compare_digest(
        compute_webhook_signature(secret=secret, timestamp=timestamp, body=body), candidate
    )


def event_matches_filter(event: Mapping[str, Any], event_filter: Mapping[str, Any]) -> bool:
    return all(event.get(key) == expected for key, expected in event_filter.items())


# ── authorization ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkBuddyKnowledgeActor:
    """The acting WorkBuddy member (tenant + department come from the principal)."""

    user_id: int
    tenant_id: str
    department_id: str | None = None
    is_tenant_admin: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeAccess:
    permission: str | None
    rank: int
    sources: tuple[str, ...] = ()

    @property
    def can_read(self) -> bool:
        return self.rank >= PERMISSION_RANK["read"]

    @property
    def can_write(self) -> bool:
        return self.rank >= PERMISSION_RANK["write"]

    @property
    def can_admin(self) -> bool:
        return self.rank >= PERMISSION_RANK["admin"]


def resolve_knowledge_access(
    base: WorkBuddyKnowledgeBaseRow,
    *,
    user_id: int,
    department_id: str | None,
    is_tenant_admin: bool,
    acl_rows: Sequence[WorkBuddyKnowledgeAclRow] = (),
) -> KnowledgeAccess:
    """Effective permission for one member on one base (see module docstring).

    The caller resolves access on rows read with ``include_archived=False``; an
    archived base is never reachable because the repository hides it first.
    """
    rank = 0
    sources: list[str] = []
    if base.scope == "personal":
        if base.owner_user_id is not None and base.owner_user_id == user_id:
            rank = PERMISSION_RANK["admin"]
            sources.append("owner")
    elif base.scope == "department":
        if department_id is not None and base.department_id == department_id:
            rank = max(rank, PERMISSION_RANK["read"])
            sources.append("department-member")
        if is_tenant_admin:
            rank = max(rank, PERMISSION_RANK["admin"])
            sources.append("tenant-admin")
    elif base.scope == "enterprise":
        rank = max(rank, PERMISSION_RANK["read"])
        sources.append("enterprise-member")
        if is_tenant_admin:
            rank = max(rank, PERMISSION_RANK["admin"])
            sources.append("tenant-admin")
    for row in acl_rows:
        subject_matches = (row.user_id is not None and row.user_id == user_id) or (
            row.department_id is not None
            and department_id is not None
            and row.department_id == department_id
        )
        if not subject_matches:
            continue
        row_rank = PERMISSION_RANK.get(row.permission, 0)
        rank = max(rank, row_rank)
        sources.append(f"acl:{row.permission}")
    if rank == 0:
        return KnowledgeAccess(None, 0, tuple(sources))
    for name, value in sorted(PERMISSION_RANK.items(), key=lambda item: -item[1]):
        if rank >= value:
            return KnowledgeAccess(name, rank, tuple(sources))
    return KnowledgeAccess(None, 0, tuple(sources))


# ── knowledge service ────────────────────────────────────────────────────────


@dataclass(slots=True)
class WorkBuddyKnowledgeService:
    """Scoped knowledge bases, bound uploads, atomic generations and search."""

    db: DatabasePool
    hooks: KnowledgeHooks | None = None
    clock: Callable[[], float] = time.time
    repo: WorkBuddyKnowledgeRepo | None = None
    jobs: RuntimeJobRecorder | None = None

    def __post_init__(self) -> None:
        self.repo = self.repo or WorkBuddyKnowledgeRepo(self.db)

    def _jobs(self, actor: WorkBuddyKnowledgeActor) -> RuntimeJobRecorder:
        # Indexing is a job (contract §4.6.2): the document's ``job_id`` is the id
        # of a row in the tenant's jobs, not a bare uuid that resolves nowhere.
        if self.jobs is not None:
            return self.jobs
        return RuntimeJobRecorder(self.db, actor.tenant_id, actor.user_id)

    # -- plumbing ----------------------------------------------------------

    @property
    def _hooks(self) -> KnowledgeHooks:
        return self.hooks if self.hooks is not None else current_workbuddy_knowledge_hooks()

    def _now(self) -> int:
        return int(self.clock())

    def context(self, actor: WorkBuddyKnowledgeActor) -> WorkBuddyDbContext:
        return WorkBuddyDbContext.for_tenant(
            actor.tenant_id, user_id=actor.user_id, department_id=actor.department_id
        )

    def _repository(self) -> WorkBuddyKnowledgeRepo:
        assert self.repo is not None
        return self.repo

    def _accessible_base(
        self,
        ctx: WorkBuddyDbContext,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
    ) -> tuple[WorkBuddyKnowledgeBaseRow, KnowledgeAccess]:
        repo = self._repository()
        base = repo.get_base(ctx, kb_id, include_archived=False)
        if base is None:
            raise OctopError(ErrorCode.NOT_FOUND, "knowledge base not found")
        rows = repo.effective_acl(
            ctx, kb_id, user_id=actor.user_id, department_id=actor.department_id
        )
        access = resolve_knowledge_access(
            base,
            user_id=actor.user_id,
            department_id=actor.department_id,
            is_tenant_admin=actor.is_tenant_admin,
            acl_rows=rows,
        )
        return base, access

    def _require(
        self,
        ctx: WorkBuddyDbContext,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        permission: str,
    ) -> tuple[WorkBuddyKnowledgeBaseRow, KnowledgeAccess]:
        base, access = self._accessible_base(ctx, actor, kb_id)
        needed = PERMISSION_RANK[permission]
        if access.rank >= needed:
            return base, access
        if not access.can_read:
            # Invisible and unauthorized look identical from outside.
            raise OctopError(ErrorCode.NOT_FOUND, "knowledge base not found")
        raise OctopError(
            ErrorCode.FORBIDDEN,
            f"knowledge base requires {permission} permission",
        )

    # -- base CRUD ---------------------------------------------------------

    def list_bases(self, actor: WorkBuddyKnowledgeActor) -> list[dict[str, Any]]:
        ctx = self.context(actor)
        repo = self._repository()
        bases = repo.list_bases(ctx, limit=MAX_LIST_BASES)
        acl_map = repo.effective_acl_map(
            ctx,
            [base.kb_id for base in bases],
            user_id=actor.user_id,
            department_id=actor.department_id,
        )
        visible: list[dict[str, Any]] = []
        for base in bases:
            access = resolve_knowledge_access(
                base,
                user_id=actor.user_id,
                department_id=actor.department_id,
                is_tenant_admin=actor.is_tenant_admin,
                acl_rows=acl_map.get(base.kb_id, ()),
            )
            if access.can_read:
                visible.append(self._base_view(base, access))
        return visible

    def create_base(
        self,
        actor: WorkBuddyKnowledgeActor,
        *,
        scope: str,
        name: str,
        description: str = "",
        department_id: str | None = None,
        model_revision_id: str,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        repo = self._repository()
        if scope not in KNOWLEDGE_SCOPES:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown knowledge base scope")
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > 120:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "knowledge base name is required"
            )
        owner_user_id: int | None = None
        target_department: str | None = None
        if scope == "personal":
            owner_user_id = actor.user_id
        elif scope == "department":
            if department_id is None:
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                    "department scope requires department_id",
                )
            if not actor.is_tenant_admin and actor.department_id != department_id:
                raise OctopError(
                    ErrorCode.FORBIDDEN,
                    "only the department's members or a tenant admin may create its knowledge base",
                )
            if not repo.department_exists(ctx, department_id):
                raise OctopError(ErrorCode.NOT_FOUND, "department not found")
            target_department = department_id
        else:
            if not actor.is_tenant_admin:
                raise OctopError(
                    ErrorCode.FORBIDDEN,
                    "only a tenant admin may create an enterprise knowledge base",
                )
        revision = self._require_published_model(ctx, model_revision_id)
        base = repo.create_base(
            ctx,
            scope=scope,
            name=clean_name,
            description=(description or "").strip()[:2000],
            model=revision,
            created_by_user_id=actor.user_id,
            owner_user_id=owner_user_id,
            department_id=target_department,
        )
        return self._base_view(
            base, KnowledgeAccess("admin", PERMISSION_RANK["admin"], ("creator",))
        )

    def get_base(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> dict[str, Any]:
        ctx = self.context(actor)
        base, access = self._require(ctx, actor, kb_id, "read")
        return self._base_view(base, access)

    def archive_base(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> dict[str, Any]:
        ctx = self.context(actor)
        base, _ = self._require(ctx, actor, kb_id, "admin")
        archived = self._repository().archive_base(
            ctx, base.kb_id, archived_by_user_id=actor.user_id
        )
        if not archived:
            raise OctopError(ErrorCode.NOT_FOUND, "knowledge base not found")
        return {"kb_id": base.kb_id, "archived": True, "archived_at": self._now()}

    # -- ACL ---------------------------------------------------------------

    def list_acl(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> list[dict[str, Any]]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "admin")
        return [self._acl_view(row) for row in self._repository().list_acl(ctx, kb_id)]

    def add_acl(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        *,
        permission: str,
        user_id: int | None = None,
        department_id: str | None = None,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "admin")
        if (user_id is None) == (department_id is None):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "exactly one of user_id or department_id is required",
            )
        if permission not in PERMISSION_RANK:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown permission level")
        repo = self._repository()
        if user_id is not None and not repo.tenant_member_exists(ctx, user_id):
            raise OctopError(ErrorCode.NOT_FOUND, "tenant member not found")
        if department_id is not None and not repo.department_exists(ctx, department_id):
            raise OctopError(ErrorCode.NOT_FOUND, "department not found")
        try:
            row = repo.add_acl(
                ctx,
                kb_id,
                permission=permission,
                granted_by_user_id=actor.user_id,
                user_id=user_id,
                department_id=department_id,
            )
        except Exception as exc:  # unique (tenant_id, kb_id, subject) violation
            if "wb_knowledge_acl" not in str(exc):
                raise
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "this subject already has an explicit grant on the knowledge base",
            ) from exc
        return self._acl_view(row)

    def update_acl(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        acl_id: str,
        *,
        permission: str,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "admin")
        if permission not in PERMISSION_RANK:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown permission level")
        row = self._repository().update_acl_permission(ctx, kb_id, acl_id, permission=permission)
        if row is None:
            raise OctopError(ErrorCode.NOT_FOUND, "grant not found")
        return self._acl_view(row)

    def delete_acl(self, actor: WorkBuddyKnowledgeActor, kb_id: str, acl_id: str) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "admin")
        if not self._repository().delete_acl(ctx, kb_id, acl_id):
            raise OctopError(ErrorCode.NOT_FOUND, "grant not found")
        return {"acl_id": acl_id, "revoked": True}

    # -- bound uploads -----------------------------------------------------

    def create_upload(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        *,
        filename: str,
        mime_type: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        store = require_object_store(self._hooks.object_store)
        clean_name = sanitize_filename(filename)
        if size_bytes <= 0 or size_bytes > MAX_UPLOAD_BYTES:
            raise OctopError(
                ErrorCode.KNOWLEDGE_DOC_TOO_LARGE,
                f"upload must be between 1 and {MAX_UPLOAD_BYTES} bytes",
            )
        expected = canonical_mime(clean_name)
        if expected is None:
            raise OctopError(
                ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
                "file type is not supported for knowledge indexing",
            )
        if mime_type != expected:
            raise OctopError(
                ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
                "declared media type does not match the file extension",
            )
        upload_id = str(uuid.uuid4())
        target = store.allocate_target(
            tenant_id=ctx.tenant_id or "",
            kb_id=kb_id,
            upload_id=upload_id,
            filename=clean_name,
        )
        upload = self._repository().create_upload(
            ctx,
            kb_id,
            requested_by_user_id=actor.user_id,
            filename=clean_name,
            mime_type=expected,
            size_bytes=size_bytes,
            object_key=target.object_key,
            ttl_seconds=UPLOAD_TTL_SECONDS,
            upload_id=upload_id,
        )
        return {
            "upload_id": upload.upload_id,
            "kb_id": upload.kb_id,
            "filename": upload.filename,
            "mime_type": upload.mime_type,
            "size_bytes": upload.size_bytes,
            "status": upload.status,
            "expires_at": upload.expires_at,
            "upload_target": {
                "object_key": target.object_key,
                "upload_url": target.upload_url,
            },
        }

    def complete_upload(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        upload_id: str,
        *,
        checksum_sha256: str | None = None,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        repo = self._repository()
        upload = repo.get_upload(ctx, kb_id, upload_id)
        if upload is None or upload.requested_by_user_id != actor.user_id:
            raise OctopError(ErrorCode.NOT_FOUND, "upload not found")
        if upload.status != "pending":
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "upload is not pending")
        if upload.expires_at < self._now():
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="UPLOAD_EXPIRED")
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "upload expired")
        store = require_object_store(self._hooks.object_store)
        scanner = require_scanner(self._hooks.scanner)
        stat = store.stat(upload.object_key)
        if stat is None:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="OBJECT_MISSING")
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "uploaded object is missing")
        if stat.size_bytes != upload.size_bytes:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="SIZE_MISMATCH")
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "uploaded object size does not match the declared size",
            )
        if upload.size_bytes > MAX_UPLOAD_BYTES:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="SIZE_LIMIT")
            raise OctopError(ErrorCode.KNOWLEDGE_DOC_TOO_LARGE, "upload is too large")
        data = store.read(upload.object_key, limit=upload.size_bytes + 1)
        digest = sha256_hex(data)
        try:
            detected = verify_upload_content(
                filename=upload.filename,
                declared_mime=upload.mime_type,
                data=data,
                declared_size=upload.size_bytes,
            )
        except (HookRejection, OctopError) as exc:
            repo.reject_upload(
                ctx, kb_id, upload_id, rejection_code=_rejection_code(exc, "CONTENT_REJECTED")
            )
            raise
        if stat.checksum_sha256 is not None and stat.checksum_sha256.lower() != digest:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="CHECKSUM_MISMATCH")
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "object checksum does not match the stored object",
            )
        if checksum_sha256 is not None and checksum_sha256.lower() != digest:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="CHECKSUM_MISMATCH")
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "upload checksum does not match the uploaded object",
            )
        try:
            verdict = scanner.scan(data, filename=upload.filename, mime_type=detected)
        except Exception as exc:
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="SCANNER_FAILED")
            raise OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, "content scanner failed") from exc
        if verdict is None or verdict.status != "clean":
            repo.reject_upload(ctx, kb_id, upload_id, rejection_code="SCANNER_REJECTED")
            raise OctopError(
                ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE,
                "content scanner did not clear the uploaded object",
            )
        file_ref = repo.complete_upload(
            ctx,
            upload,
            checksum_sha256=digest,
            detected_mime=detected,
            scan_status=verdict.status,
            completed_by_user_id=actor.user_id,
        )
        return self._file_ref_view(file_ref)

    # -- documents ---------------------------------------------------------

    def create_document(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        *,
        title: str,
        source: str = DOCUMENT_SOURCE_UPLOAD,
        file_ref_id: str | None = None,
        upload_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a document row.

        ``source='upload'`` is the original path: the document names a completed
        file reference and an object store holds its bytes.  The text-only sources
        (``'text'``, ``'migration'``) belong to content that has no source file —
        a document created from supplied text, or one imported from the personal
        edition, whose ``index.sqlite`` keeps chunk text and vectors only — so they
        must not name a file reference, and their content is supplied to
        :meth:`index_text_document` instead of being read back from storage.
        """
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        repo = self._repository()
        text_only = validate_document_source(source)
        if text_only:
            if file_ref_id is not None or upload_id is not None:
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                    "a text-only document cannot name a file reference",
                )
            ref: WorkBuddyKnowledgeFileRefRow | None = None
        else:
            if not file_ref_id:
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                    "file_ref is required for an uploaded document",
                )
            ref = repo.get_file_ref(ctx, kb_id, file_ref_id)
            if ref is None:
                raise OctopError(ErrorCode.NOT_FOUND, "file reference not found")
            if upload_id is not None and ref.upload_id != upload_id:
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                    "file reference does not belong to the upload",
                )
        clean_title = sanitize_filename(title or (ref.filename if ref is not None else ""))
        jobs = self._jobs(actor)
        job_id = jobs.start(
            kind="knowledge_index",
            request={
                "kb_id": kb_id,
                "source": source,
                **({} if ref is None else {"file_ref_id": ref.file_ref_id}),
                "title": clean_title,
            },
        )
        try:
            document = repo.create_document(
                ctx,
                kb_id,
                file_ref_id=None if ref is None else ref.file_ref_id,
                source=source,
                title=clean_title,
                created_by_user_id=actor.user_id,
                job_id=job_id,
            )
        except Exception as exc:
            jobs.finish(
                job_id,
                status="failed",
                error_code="DOCUMENT_CREATE_FAILED",
                error_message=str(exc),
            )
            raise
        return self._document_view(document)

    def list_documents(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> list[dict[str, Any]]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "read")
        return [self._document_view(row) for row in self._repository().list_documents(ctx, kb_id)]

    def delete_document(
        self, actor: WorkBuddyKnowledgeActor, kb_id: str, document_id: str
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        if not self._repository().soft_delete_document(ctx, kb_id, document_id):
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        return {"document_id": document_id, "deleted": True, "retrievable": False}

    # -- indexing (background) --------------------------------------------

    def index_document(
        self,
        *,
        tenant_id: str,
        actor_user_id: int,
        kb_id: str,
        document_id: str,
        department_id: str | None = None,
    ) -> None:
        """Parse, embed and atomically publish one *uploaded* document's generation.

        A text-only document has no object to parse; :meth:`index_text_document`
        indexes it from the text its caller supplies.
        """
        actor = WorkBuddyKnowledgeActor(
            user_id=actor_user_id, tenant_id=tenant_id, department_id=department_id
        )
        ctx = self.context(actor)
        repo = self._repository()
        base = repo.get_base(ctx, kb_id, include_archived=False)
        document = repo.get_document(ctx, kb_id, document_id)
        if base is None or document is None or document.deleted_at is not None:
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        if document.source in TEXT_DOCUMENT_SOURCES:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "document has no source file; index its text instead",
            )
        if document.file_ref_id is None:  # the source shape constraint forbids this
            raise OctopError(ErrorCode.NOT_FOUND, "file reference not found")
        ref = repo.get_file_ref(ctx, kb_id, document.file_ref_id)
        if ref is None:
            raise OctopError(ErrorCode.NOT_FOUND, "file reference not found")
        jobs = self._jobs(actor)
        jobs.begin(document.job_id)
        try:
            store = require_object_store(self._hooks.object_store)
            parser = require_parser(self._hooks.parser)
            repo.set_document_status(ctx, kb_id, document_id, status="parsing")
            data = store.read(ref.object_key, limit=min(ref.size_bytes, MAX_UPLOAD_BYTES) + 1)
            if sha256_hex(data) != ref.checksum_sha256:
                raise HookRejection(
                    "CHECKSUM_MISMATCH", "stored object no longer matches its checksum"
                )
            parsed = parser.parse(data, filename=ref.filename, mime_type=ref.mime_type)
            validate_archive_manifest(parsed.archive_entries)
            generation = self._publish_parsed(
                ctx=ctx,
                repo=repo,
                base=base,
                document=document,
                actor_user_id=actor_user_id,
                parsed=parsed,
                source_label=ref.filename,
            )
        except Exception as exc:
            code = (
                exc.code.value
                if isinstance(exc, OctopError)
                else str(getattr(exc, "code", "INDEX_FAILED"))
            )
            repo.set_document_status(ctx, kb_id, document_id, status="failed", error_code=code)
            jobs.finish(document.job_id, status="failed", error_code=code, error_message=str(exc))
            raise
        jobs.finish(
            document.job_id,
            status="succeeded",
            result={
                "document_id": document_id,
                "kb_id": kb_id,
                "generation_id": generation.generation_id,
                "chunk_count": generation.chunk_count,
            },
        )

    def index_text_document(
        self,
        *,
        tenant_id: str,
        actor_user_id: int,
        kb_id: str,
        document_id: str,
        text: str,
        department_id: str | None = None,
    ) -> None:
        """Chunk, embed and atomically publish a text-only document's generation.

        The content is supplied by the caller — a migration import or a caller
        that pasted text — because there is no source file to read back.  It is
        never stored as a blob: only the chunks and their vectors are persisted,
        which is exactly what the personal edition kept.
        """
        actor = WorkBuddyKnowledgeActor(
            user_id=actor_user_id, tenant_id=tenant_id, department_id=department_id
        )
        ctx = self.context(actor)
        repo = self._repository()
        base = repo.get_base(ctx, kb_id, include_archived=False)
        document = repo.get_document(ctx, kb_id, document_id)
        if base is None or document is None or document.deleted_at is not None:
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        if document.source not in TEXT_DOCUMENT_SOURCES:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "document has a source file; index it from storage",
            )
        jobs = self._jobs(actor)
        jobs.begin(document.job_id)
        try:
            content = validate_document_text(text)
            repo.set_document_status(ctx, kb_id, document_id, status="parsing")
            parsed = ParsedDocument(text_blocks=(TextBlock(text=content),))
            generation = self._publish_parsed(
                ctx=ctx,
                repo=repo,
                base=base,
                document=document,
                actor_user_id=actor_user_id,
                parsed=parsed,
                source_label=document.title,
            )
        except Exception as exc:
            code = (
                exc.code.value
                if isinstance(exc, OctopError)
                else str(getattr(exc, "code", "INDEX_FAILED"))
            )
            repo.set_document_status(ctx, kb_id, document_id, status="failed", error_code=code)
            jobs.finish(document.job_id, status="failed", error_code=code, error_message=str(exc))
            raise
        jobs.finish(
            document.job_id,
            status="succeeded",
            result={
                "document_id": document_id,
                "kb_id": kb_id,
                "generation_id": generation.generation_id,
                "chunk_count": generation.chunk_count,
            },
        )

    def default_open_bases(self, actor: WorkBuddyKnowledgeActor) -> dict[str, Any]:
        """The bases this member opens by default (the personal edition's flag)."""
        ctx = self.context(actor)
        opened = self._repository().default_open_bases(ctx, user_id=actor.user_id)
        return {"kb_ids": sorted(kb_id for kb_id, flag in opened.items() if flag)}

    def set_default_open(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        *,
        default_open: bool,
    ) -> dict[str, Any]:
        """Open (or close) one base for the calling member only."""
        ctx = self.context(actor)
        base, _access = self._require(ctx, actor, kb_id, "read")
        if not self._repository().set_default_open(
            ctx, base.kb_id, user_id=actor.user_id, default_open=default_open
        ):
            raise OctopError(ErrorCode.NOT_FOUND, "knowledge base not found")
        return {"kb_id": base.kb_id, "default_open": bool(default_open)}

    def list_folders(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> dict[str, Any]:
        """The folders of one base with their live document counts.

        The root is always reported, even when empty, so a client can render the
        base itself without special-casing a missing entry.
        """
        ctx = self.context(actor)
        base, _access = self._require(ctx, actor, kb_id, "read")
        titled = []
        for path, held in self._repository().list_folders(ctx, kb_id):
            titled.append({"path": path, "document_count": held})
        return {"kb_id": base.kb_id, "folders": titled}

    def move_document(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        document_id: str,
        *,
        folder_path: str,
    ) -> dict[str, Any]:
        """Move one document into a folder (or back to the root)."""
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        target = normalize_folder_path(folder_path)
        if not self._repository().move_document(ctx, kb_id, document_id, folder_path=target):
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        return {"document_id": document_id, "kb_id": kb_id, "folder_path": target}

    def rename_document(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        document_id: str,
        *,
        title: str,
    ) -> dict[str, Any]:
        """Replace one document's title (the same 1–255 bound its row enforces)."""
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        clean_title = (title or "").strip()
        if not 1 <= len(clean_title) <= DOCUMENT_TITLE_MAX_LENGTH:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                f"document title must be 1 to {DOCUMENT_TITLE_MAX_LENGTH} characters",
            )
        if not self._repository().rename_document(ctx, kb_id, document_id, title=clean_title):
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        return {"document_id": document_id, "kb_id": kb_id, "title": clean_title}

    def document_text(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        document_id: str,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """The indexed text of one document; ``limit`` yields a preview.

        This is the text the personal edition could hand back: a text-only or
        migrated document has no source file, so its chunks are the content. The
        preview and the text export read exactly this.
        """
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "read")
        repo = self._repository()
        document = repo.get_document(ctx, kb_id, document_id)
        if document is None or document.deleted_at is not None:
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        text = "\n\n".join(repo.active_chunk_texts(ctx, kb_id, document_id))
        truncated = False
        if limit is not None and len(text) > max(0, int(limit)):
            text = text[: max(0, int(limit))]
            truncated = True
        return {
            "document_id": document.document_id,
            "kb_id": kb_id,
            "title": document.title,
            "text": text,
            "truncated": truncated,
            "chunk_count": int(document.chunk_count),
        }

    def reindex_document(
        self, actor: WorkBuddyKnowledgeActor, kb_id: str, document_id: str
    ) -> dict[str, Any]:
        """Publish a fresh generation for one document (per-document reindex).

        A text-only or migrated document is re-indexed from the text its chunks
        already hold; an uploaded one is parsed again from storage. Either way the
        document keeps its job row, and the previous generation stays until the
        new one is published atomically.
        """
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        repo = self._repository()
        document = repo.get_document(ctx, kb_id, document_id)
        if document is None or document.deleted_at is not None:
            raise OctopError(ErrorCode.NOT_FOUND, "document not found")
        if document.source in TEXT_DOCUMENT_SOURCES:
            text = "\n\n".join(repo.active_chunk_texts(ctx, kb_id, document_id))
            if not text.strip():
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                    "document has no indexed text to reindex",
                )
            self.index_text_document(
                tenant_id=actor.tenant_id,
                actor_user_id=actor.user_id,
                kb_id=kb_id,
                document_id=document_id,
                text=text,
                department_id=actor.department_id,
            )
        else:
            self.index_document(
                tenant_id=actor.tenant_id,
                actor_user_id=actor.user_id,
                kb_id=kb_id,
                document_id=document_id,
                department_id=actor.department_id,
            )
        return {
            "document_id": document_id,
            "kb_id": kb_id,
            "job_id": str(document.job_id),
            "reindexed": True,
        }

    def reindex_base(self, actor: WorkBuddyKnowledgeActor, kb_id: str) -> dict[str, Any]:
        """Reindex every live document of one base, reporting each refusal.

        One unreadable document must not stop the rest: the failures come back as
        codes the caller can act on, and each document records its own job.
        """
        ctx = self.context(actor)
        self._require(ctx, actor, kb_id, "write")
        repo = self._repository()
        queued = 0
        failed: list[dict[str, str]] = []
        for row in repo.list_documents(ctx, kb_id):
            try:
                self.reindex_document(actor, kb_id, row.document_id)
                queued += 1
            except OctopError as exc:
                failed.append({"document_id": row.document_id, "code": exc.code.value})
        return {"kb_id": kb_id, "queued": queued, "failed": failed}

    def _publish_parsed(
        self,
        *,
        ctx: WorkBuddyDbContext,
        repo: Any,
        base: Any,
        document: WorkBuddyKnowledgeDocumentRow,
        actor_user_id: int,
        parsed: ParsedDocument,
        source_label: str,
    ) -> Any:
        """Chunk, embed and publish one document's ready generation (shared tail)."""
        chunks = chunk_parsed_text(parsed)
        if not chunks:
            raise HookRejection("NO_TEXT", "document contains no extractable text")
        embedder = require_embedder(self._hooks.embedder)
        descriptor = self._require_descriptor(ctx, base, embedder)
        vectors = embedder.embed([chunk for chunk, _ in chunks])
        if not isinstance(vectors, list) or len(vectors) != len(chunks):
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "embedder returned a different number of vectors than chunks",
            )
        prepared: list[tuple[int, str, int, dict[str, Any], list[float]]] = []
        for ordinal, ((text, tokens), raw_vector) in enumerate(zip(chunks, vectors, strict=True)):
            prepared.append(
                (
                    ordinal,
                    text,
                    tokens,
                    {"source": source_label, "model": descriptor.model_key},
                    validate_embedding_vector(raw_vector, dimensions=base.embedding_dimensions),
                )
            )
        repo.set_document_status(ctx, base.kb_id, document.document_id, status="indexing")
        return repo.publish_generation(
            ctx,
            base=base,
            document_id=document.document_id,
            created_by_user_id=actor_user_id,
            chunks=prepared,
        )

    # -- search ------------------------------------------------------------

    def search(
        self,
        actor: WorkBuddyKnowledgeActor,
        kb_id: str,
        *,
        query: str,
        match_count: int = 5,
        embedding_model: str | None = None,
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        base, _ = self._require(ctx, actor, kb_id, "read")
        text = validate_query_text(query)
        if embedding_model is not None and embedding_model != base.embedding_model_key:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "embedding_model must match the knowledge base's pinned model",
            )
        if match_count < 1 or match_count > MAX_MATCH_COUNT:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                f"match_count must be between 1 and {MAX_MATCH_COUNT}",
            )
        embedder = require_embedder(self._hooks.embedder)
        self._require_descriptor(ctx, base, embedder)
        vectors = embedder.embed([text])
        if not isinstance(vectors, list) or len(vectors) != 1:
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED, "embedder did not return exactly one vector"
            )
        vector = validate_embedding_vector(vectors[0], dimensions=base.embedding_dimensions)
        hits = self._repository().search_chunks(
            ctx, base.kb_id, query_vector=vector, limit=match_count
        )
        return {
            "kb_id": base.kb_id,
            "embedding": {
                "adapter_key": base.embedding_adapter_key,
                "model_key": base.embedding_model_key,
                "revision": base.embedding_revision,
                "dimensions": base.embedding_dimensions,
            },
            "match_count": len(hits),
            "hits": [
                {
                    "chunk_id": hit.chunk_id,
                    "document_id": hit.document_id,
                    "document_title": hit.document_title,
                    "generation_id": hit.generation_id,
                    "ordinal": hit.ordinal,
                    "content": hit.content,
                    "score": hit.score,
                }
                for hit in hits
            ],
        }

    # -- model pin ---------------------------------------------------------

    def _require_published_model(
        self, ctx: WorkBuddyDbContext, model_revision_id: str
    ) -> WorkBuddyPlatformModelRevisionRow:
        repo = self._repository()
        revision = repo.get_platform_model_revision(ctx, model_revision_id)
        if revision is None:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown model revision")
        if revision.status != "published":
            raise OctopError(
                ErrorCode.WORKBUDDY_PLATFORM_REVISION_REVOKED,
                "model revision is revoked",
            )
        if revision.model_key != BGE_M3_MODEL_KEY and not _pinnable_local_model(revision):
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                f"only {BGE_M3_MODEL_KEY} or a {VECTOR_DIMENSIONS}-dimension"
                f" {ADAPTER_KEY_ONNX} revision declaring its local model id"
                " can back a knowledge base",
            )
        if not repo.tenant_model_granted(ctx, revision.model_revision_id):
            raise OctopError(
                ErrorCode.WORKBUDDY_CAPABILITY_NOT_APPROVED,
                "the tenant is not granted this model revision",
            )
        return revision

    def _require_descriptor(
        self,
        ctx: WorkBuddyDbContext,
        base: WorkBuddyKnowledgeBaseRow,
        embedder: EmbeddingHook,
    ) -> EmbeddingDescriptor:
        """The runtime embedder must be the base's pinned, granted revision."""
        self._require_published_model(ctx, base.embedding_model_revision_id)
        try:
            descriptor = embedder.describe()
        except Exception as exc:
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED, "embedding model probe failed"
            ) from exc
        pinned = (
            base.embedding_adapter_key,
            base.embedding_model_key,
            base.embedding_revision,
            base.embedding_dimensions,
        )
        reported = (
            (
                descriptor.adapter_key,
                descriptor.model_key,
                descriptor.revision,
                descriptor.dimensions,
            )
            if descriptor is not None
            else None
        )
        if descriptor is None or reported != pinned:
            raise OctopError(
                ErrorCode.MODEL_NOT_CONFIGURED,
                "embedder does not match the knowledge base's pinned model revision",
            )
        return descriptor

    # -- views -------------------------------------------------------------

    def _base_view(
        self, base: WorkBuddyKnowledgeBaseRow, access: KnowledgeAccess
    ) -> dict[str, Any]:
        return {
            "kb_id": base.kb_id,
            "name": base.name,
            "description": base.description,
            "scope": base.scope,
            "department_id": base.department_id,
            "owner_user_id": base.owner_user_id,
            "archived_at": base.archived_at,
            "permission": access.permission,
            # Why *this caller* can read the base, in resolver order: owner /
            # department-member / enterprise-member / tenant-admin / acl:<perm>.
            # It names the caller's own sources only, never another member's.
            "access_sources": list(access.sources),
            "embedding": {
                "adapter_key": base.embedding_adapter_key,
                "model_key": base.embedding_model_key,
                "revision": base.embedding_revision,
                "model_revision_id": base.embedding_model_revision_id,
                "dimensions": base.embedding_dimensions,
            },
            "created_at": base.created_at,
            "updated_at": base.updated_at,
        }

    def _acl_view(self, row: WorkBuddyKnowledgeAclRow) -> dict[str, Any]:
        return {
            "acl_id": row.acl_id,
            "user_id": row.user_id,
            "department_id": row.department_id,
            "permission": row.permission,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _file_ref_view(self, ref: Any) -> dict[str, Any]:
        return {
            "file_ref_id": ref.file_ref_id,
            "kb_id": ref.kb_id,
            "upload_id": ref.upload_id,
            "filename": ref.filename,
            "mime_type": ref.mime_type,
            "size_bytes": ref.size_bytes,
            "checksum_sha256": ref.checksum_sha256,
            "created_at": ref.created_at,
        }

    def _document_view(self, row: WorkBuddyKnowledgeDocumentRow) -> dict[str, Any]:
        return {
            "document_id": row.document_id,
            "kb_id": row.kb_id,
            "file_ref_id": row.file_ref_id,
            "source": row.source,
            "title": row.title,
            "status": row.status,
            "error_code": row.error_code,
            "active_generation_id": row.active_generation_id,
            "chunk_count": row.chunk_count,
            "folder_path": row.folder_path,
            "job_id": row.job_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def _rejection_code(exc: BaseException, default: str) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, ErrorCode):
        return code.value
    if isinstance(code, str) and code:
        return code
    return default


def validate_document_source(source: Any) -> bool:
    """Return True for a text-only source, False for an uploaded one."""
    value = str(source or "").strip().lower()
    if value not in DOCUMENT_SOURCES:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "source must be one of: " + ", ".join(DOCUMENT_SOURCES),
        )
    return value in TEXT_DOCUMENT_SOURCES


FOLDER_SEPARATOR = "/"


def normalize_folder_path(value: Any) -> str:
    """Validate a folder path and return its canonical form (``""`` is the root).

    The same rules the database check enforces: relative segments only, no
    traversal, no backslash, single separators, no surrounding spaces. An empty
    string is the base root rather than a folder of its own.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith(FOLDER_SEPARATOR) or text.endswith(FOLDER_SEPARATOR):
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "folder path must be relative")
    segments = text.split(FOLDER_SEPARATOR)
    for segment in segments:
        if segment in ("", ".", ".."):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "folder path has an empty or relative segment"
            )
        if "\\" in segment:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "folder path cannot hold \\")
        if segment != segment.strip():
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "folder segment has stray spaces"
            )
    return FOLDER_SEPARATOR.join(segments)


def validate_document_text(text: Any) -> str:
    """Return the content of a text-only document, or refuse an unusable one."""
    value = str(text or "")
    if not value.strip():
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "the document text must not be empty"
        )
    if len(value) > MAX_UPLOAD_BYTES:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            f"the document text must be at most {MAX_UPLOAD_BYTES} characters",
        )
    return value


def chunk_parsed_text(
    parsed: ParsedDocument, *, size: int = 800, overlap: int = 120
) -> list[tuple[str, int]]:
    """Deterministic overlapping windows with an approximate token count."""
    from octop.infra.knowledge.chunk import chunk_text

    pieces = chunk_text(parsed.text, size=size, overlap=overlap)
    return [(piece, len(piece.split())) for piece in pieces]


# ── trigger service ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class WorkBuddyTriggerService:
    """Trigger registrations, secret rotation and authenticated webhook intake."""

    db: DatabasePool
    hooks: TriggerHooks | None = None
    clock: Callable[[], float] = time.time
    repo: WorkBuddyTriggerRepo | None = None
    knowledge_repo: WorkBuddyKnowledgeRepo | None = None

    def __post_init__(self) -> None:
        self.repo = self.repo or WorkBuddyTriggerRepo(self.db)
        self.knowledge_repo = self.knowledge_repo or WorkBuddyKnowledgeRepo(self.db)

    @property
    def _hooks(self) -> TriggerHooks:
        return self.hooks if self.hooks is not None else current_workbuddy_trigger_hooks()

    def _now(self) -> int:
        return int(self.clock())

    def context(self, actor: WorkBuddyKnowledgeActor) -> WorkBuddyDbContext:
        return WorkBuddyDbContext.for_tenant(
            actor.tenant_id, user_id=actor.user_id, department_id=actor.department_id
        )

    def _repository(self) -> WorkBuddyTriggerRepo:
        assert self.repo is not None
        return self.repo

    def _knowledge(self) -> WorkBuddyKnowledgeRepo:
        assert self.knowledge_repo is not None
        return self.knowledge_repo

    def _require_registration(
        self,
        ctx: WorkBuddyDbContext,
        registration_id: str,
        *,
        workflow_id: str | None = None,
    ) -> WorkBuddyTriggerRegistrationRow:
        registration = self._repository().get_registration(ctx, registration_id)
        if registration is None or (
            workflow_id is not None and registration.workflow_id != workflow_id
        ):
            raise OctopError(ErrorCode.NOT_FOUND, "trigger registration not found")
        return registration

    def create_registration(
        self,
        actor: WorkBuddyKnowledgeActor,
        workflow_id: str,
        *,
        kind: str,
        name: str,
        cron_expression: str | None = None,
        event_name: str | None = None,
        event_filter: Mapping[str, Any] | None = None,
        tool_grants: Sequence[str] = (),
        kb_grants: Sequence[tuple[str, str]] = (),
        tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
        signature_header: str = "x-workbuddy-signature",
        timestamp_header: str = "x-workbuddy-timestamp",
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        repo = self._repository()
        clean_name = (name or "").strip()
        if not clean_name or len(clean_name) > 120:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "registration name is required")
        if kind not in {"cron", "webhook", "event"}:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown trigger kind")
        if not repo.workflow_exists(ctx, workflow_id):
            raise OctopError(ErrorCode.NOT_FOUND, "workflow not found")
        if kind == "cron" and not (cron_expression or "").strip():
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "cron registrations require cron_expression"
            )
        if kind == "event" and not (event_name or "").strip():
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "event registrations require event_name"
            )
        if tolerance_seconds < 30 or tolerance_seconds > 3600:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "tolerance_seconds is out of range"
            )
        webhook_path: str | None = None
        secret_provider: str | None = None
        secret_ref: str | None = None
        secret_version = 0
        if kind == "webhook":
            backend = self._require_secret_backend()
            webhook_path = secrets.token_urlsafe(24).replace("=", "")[:48]
            if not _WEBHOOK_PATH.fullmatch(webhook_path):
                raise OctopError(ErrorCode.INTERNAL_ERROR, "generated webhook path is invalid")
            secret_value = secrets.token_urlsafe(32)
            register_secret(secret_value)
            secret_ref = backend.create_secret(
                name=f"workbuddy/{ctx.tenant_id}/{webhook_path}", value=secret_value
            )
            secret_provider = type(backend).__name__
            secret_version = 1
        registration = repo.create_registration(
            ctx,
            workflow_id=workflow_id,
            kind=kind,
            name=clean_name,
            created_by_user_id=actor.user_id,
            webhook_path=webhook_path,
            cron_expression=(cron_expression or None) if kind == "cron" else None,
            event_name=(event_name or None) if kind == "event" else None,
            event_filter=dict(event_filter or {}),
            secret_provider=secret_provider,
            secret_ref=secret_ref,
            secret_version=secret_version,
            signature_header=signature_header,
            timestamp_header=timestamp_header,
            tolerance_seconds=tolerance_seconds,
        )
        for tool_name in tool_grants:
            clean_tool = (tool_name or "").strip()
            if not clean_tool:
                raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "tool grant is empty")
            repo.add_grant(
                ctx,
                registration.registration_id,
                created_by_user_id=actor.user_id,
                tool_name=clean_tool,
            )
        for kb_id, permission in kb_grants:
            if permission not in {"read", "write"}:
                raise OctopError(
                    ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "knowledge grant permission is invalid"
                )
            base = self._knowledge().get_base(ctx, kb_id, include_archived=False)
            if base is None:
                raise OctopError(ErrorCode.NOT_FOUND, "knowledge base not found")
            repo.add_grant(
                ctx,
                registration.registration_id,
                created_by_user_id=actor.user_id,
                kb_id=kb_id,
                kb_permission=permission,
            )
        return self._registration_view(ctx, registration)

    def list_registrations(
        self, actor: WorkBuddyKnowledgeActor, workflow_id: str
    ) -> list[dict[str, Any]]:
        ctx = self.context(actor)
        if not self._repository().workflow_exists(ctx, workflow_id):
            raise OctopError(ErrorCode.NOT_FOUND, "workflow not found")
        return [
            self._registration_view(ctx, row)
            for row in self._repository().list_registrations(ctx, workflow_id)
        ]

    def revoke_registration(
        self, actor: WorkBuddyKnowledgeActor, workflow_id: str, registration_id: str
    ) -> dict[str, Any]:
        ctx = self.context(actor)
        self._require_registration(ctx, registration_id, workflow_id=workflow_id)
        if not self._repository().revoke_registration(ctx, registration_id):
            raise OctopError(ErrorCode.NOT_FOUND, "trigger registration not found")
        return {"registration_id": registration_id, "revoked": True}

    def rotate_secret(
        self, actor: WorkBuddyKnowledgeActor, workflow_id: str, registration_id: str
    ) -> dict[str, Any]:
        """Write a new secret into the external backend and return it exactly once."""
        ctx = self.context(actor)
        registration = self._require_registration(ctx, registration_id, workflow_id=workflow_id)
        if registration.kind != "webhook" or registration.webhook_path is None:
            raise OctopError(ErrorCode.NOT_FOUND, "trigger registration not found")
        backend = self._require_secret_backend()
        new_secret = secrets.token_urlsafe(32)
        register_secret(new_secret)
        now = self._now()
        reference = backend.create_secret(
            name=f"workbuddy/{ctx.tenant_id}/{registration.webhook_path}/v{registration.secret_version + 1}",
            value=new_secret,
        )
        updated = self._repository().update_registration_secret(
            ctx,
            registration.registration_id,
            secret_provider=type(backend).__name__,
            secret_ref=reference,
            secret_version=registration.secret_version + 1,
            previous_secret_ref=registration.secret_ref,
            previous_secret_expires_at=(
                now + SECRET_OVERLAP_SECONDS if registration.secret_ref else None
            ),
        )
        if updated is None:
            backend.delete_secret(reference)
            raise OctopError(ErrorCode.NOT_FOUND, "trigger registration not found")
        return {
            "registration_id": registration.registration_id,
            "secret": new_secret,
            "secret_version": updated.secret_version,
            "algorithm": updated.signature_algorithm,
            "overlap_expires_at": updated.previous_secret_expires_at,
        }

    # -- ingestion ---------------------------------------------------------

    def ingest_webhook(
        self,
        webhook_path: str,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        """Verify a signed public delivery, dedupe it by event key and dispatch once."""
        if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
            raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "webhook body is too large")
        repo = self._repository()
        registration = repo.resolve_webhook_registration(webhook_path)
        if registration is None or not registration.is_active:
            raise OctopError(ErrorCode.NOT_FOUND, "webhook not found")
        lowered = {key.lower(): value for key, value in headers.items()}
        now = self._now()
        timestamp_raw = lowered.get(registration.timestamp_header.lower(), "")
        try:
            timestamp = int(str(timestamp_raw).strip())
        except (TypeError, ValueError):
            raise OctopError(
                ErrorCode.TRIGGER_SIGNATURE_INVALID,
                "webhook timestamp header is missing or invalid",
            ) from None
        if not timestamp_in_window(
            timestamp, tolerance_seconds=registration.tolerance_seconds, now=now
        ):
            raise OctopError(
                ErrorCode.TRIGGER_SIGNATURE_INVALID,
                "webhook timestamp is outside the allowed window",
            )
        self._require_secret_backend()  # fail closed before touching the body
        provided = lowered.get(registration.signature_header.lower())
        if not self._verify_any_secret(
            registration, body=raw_body, timestamp=timestamp, provided=provided, now=now
        ):
            raise OctopError(
                ErrorCode.TRIGGER_SIGNATURE_INVALID, "webhook signature does not verify"
            )
        event = self._decode_event(raw_body)
        ctx = WorkBuddyDbContext.for_tenant(registration.tenant_id)
        event_key = self._event_key(lowered, raw_body)
        event_name = registration.event_name
        effective_filter = dict(registration.event_filter or {})
        if event_name:
            effective_filter.setdefault("event", event_name)
        claim: DeliveryClaim = repo.claim_delivery(
            ctx,
            registration.registration_id,
            event_key=event_key,
            body_sha256=sha256_hex(raw_body),
            event_name=event_name,
            signature_version=registration.secret_version,
            signature_timestamp=timestamp,
            is_test=False,
            actor_user_id=None,
        )
        if not claim.dispatch:
            return {
                "delivery_id": claim.delivery.delivery_id,
                "registration_id": registration.registration_id,
                "execution_id": claim.delivery.execution_id,
                "status": claim.delivery.status,
                "duplicate": True,
            }
        if effective_filter and not event_matches_filter(event, effective_filter):
            repo.reject_delivery(
                ctx, claim.delivery.delivery_id, rejection_code="EVENT_FILTER_MISMATCH"
            )
            return {
                "delivery_id": claim.delivery.delivery_id,
                "registration_id": registration.registration_id,
                "execution_id": None,
                "status": "rejected",
                "duplicate": False,
            }
        return self._dispatch(
            ctx, registration, claim.delivery, event_key=event_key, event=event, is_test=False
        )

    def test_delivery(self, actor: WorkBuddyKnowledgeActor, registration_id: str) -> dict[str, Any]:
        """Admin replay: dispatch a synthetic event and record the audit row."""
        ctx = self.context(actor)
        registration = self._require_registration(ctx, registration_id)
        if not registration.is_active:
            raise OctopError(ErrorCode.NOT_FOUND, "trigger registration not found")
        event_key = f"test:{uuid.uuid4().hex}"
        claim = self._repository().claim_delivery(
            ctx,
            registration.registration_id,
            event_key=event_key,
            body_sha256=sha256_hex(event_key.encode()),
            event_name=registration.event_name,
            signature_version=registration.secret_version,
            signature_timestamp=self._now(),
            is_test=True,
            actor_user_id=actor.user_id,
        )
        result = self._dispatch(
            ctx,
            registration,
            claim.delivery,
            event_key=event_key,
            event={"event": registration.event_name or "test", "test": True},
            is_test=True,
        )
        return {**result, "test": True}

    # -- internals ---------------------------------------------------------

    def _require_secret_backend(self) -> SecretBackendHook:
        backend = self._hooks.secret_backend
        if not _hook_available(backend):
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "external secret backend is not configured or not proven",
            )
        return backend  # type: ignore[return-value]

    def _verify_any_secret(
        self,
        registration: WorkBuddyTriggerRegistrationRow,
        *,
        body: bytes,
        timestamp: int,
        provided: str | None,
        now: int,
    ) -> bool:
        backend = self._require_secret_backend()
        references = [registration.secret_ref]
        if (
            registration.previous_secret_ref is not None
            and registration.previous_secret_expires_at is not None
            and registration.previous_secret_expires_at >= now
        ):
            references.append(registration.previous_secret_ref)
        for reference in references:
            if reference is None:
                continue
            try:
                secret = backend.read_secret(reference)
            except Exception as exc:
                raise OctopError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE, "external secret backend is unavailable"
                ) from exc
            if secret and signature_matches(
                secret=secret, timestamp=timestamp, body=body, provided=provided
            ):
                return True
        return False

    def _decode_event(self, raw_body: bytes) -> dict[str, Any]:
        import json

        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "webhook body must be a JSON object"
            ) from None
        if not isinstance(decoded, dict):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "webhook body must be a JSON object"
            )
        return decoded

    def _event_key(self, headers: Mapping[str, str], raw_body: bytes) -> str:
        candidate = str(headers.get(DEFAULT_EVENT_KEY_HEADER, "")).strip()
        if candidate and _EVENT_KEY.fullmatch(candidate):
            return candidate
        # Without an explicit event id the raw body hash is the stable key, so a
        # byte-identical replay can never execute twice.
        return sha256_hex(raw_body)

    def _dispatch(
        self,
        ctx: WorkBuddyDbContext,
        registration: WorkBuddyTriggerRegistrationRow,
        delivery: WorkBuddyTriggerDeliveryRow,
        *,
        event_key: str,
        event: Mapping[str, Any],
        is_test: bool,
    ) -> dict[str, Any]:
        dispatcher = self._hooks.dispatcher
        if dispatcher is None or not _hook_available(dispatcher):
            self._repository().reject_delivery(
                ctx, delivery.delivery_id, rejection_code="DISPATCHER_UNAVAILABLE"
            )
            raise OctopError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "workflow dispatcher is not configured or not proven",
            )
        try:
            execution_id = dispatcher.dispatch(
                tenant_id=registration.tenant_id,
                registration=registration,
                delivery_id=delivery.delivery_id,
                event_key=event_key,
                event=event,
                is_test=is_test,
            )
        except OctopError:
            self._repository().reject_delivery(
                ctx, delivery.delivery_id, rejection_code="DISPATCH_FAILED"
            )
            raise
        except Exception as exc:
            self._repository().reject_delivery(
                ctx, delivery.delivery_id, rejection_code="DISPATCH_FAILED"
            )
            raise OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, "workflow dispatch failed") from exc
        self._repository().complete_delivery(ctx, delivery.delivery_id, execution_id=execution_id)
        return {
            "delivery_id": delivery.delivery_id,
            "registration_id": registration.registration_id,
            "execution_id": execution_id,
            "status": "executed",
            "duplicate": False,
        }

    def _registration_view(
        self, ctx: WorkBuddyDbContext, row: WorkBuddyTriggerRegistrationRow
    ) -> dict[str, Any]:
        grants = self._repository().list_grants(ctx, row.registration_id)
        return {
            "registration_id": row.registration_id,
            "workflow_id": row.workflow_id,
            "kind": row.kind,
            "name": row.name,
            "enabled": row.enabled,
            "revoked_at": row.revoked_at,
            "webhook_path": row.webhook_path,
            "cron_expression": row.cron_expression,
            "event_name": row.event_name,
            "event_filter": row.event_filter,
            "has_secret": row.secret_ref is not None,
            "secret_provider": row.secret_provider,
            "secret_version": row.secret_version,
            "previous_secret_active_until": row.previous_secret_expires_at,
            "signature_algorithm": row.signature_algorithm,
            "signature_header": row.signature_header,
            "timestamp_header": row.timestamp_header,
            "tolerance_seconds": row.tolerance_seconds,
            "grants": {
                "tools": [grant.tool_name for grant in grants if grant.tool_name],
                "knowledge_bases": [
                    {"kb_id": grant.kb_id, "permission": grant.permission}
                    for grant in grants
                    if grant.kb_id
                ],
            },
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

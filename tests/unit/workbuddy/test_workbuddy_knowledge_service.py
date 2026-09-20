"""Focused WorkBuddy knowledge/trigger tests (no database required).

The service layer is exercised with in-memory repositories and hook doubles:

* permission precedence for personal / department / enterprise bases and ACL,
* bounded upload + archive validation (magic, size, zip bomb, path traversal),
* pinned bge-m3 revision, dimension and finite/non-zero query checks,
* atomic generation publishing and fail-closed indexing,
* webhook HMAC window, replay dedupe and secret-free metadata.

PostgreSQL behaviour (RLS, pgvector search) is covered by the migration itself;
these tests never open a database, so they also pin the SQLite fail-closed path.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from octop.infra.db.repos.workbuddy_knowledge import (
    EMBEDDING_DIMENSIONS,
    DeliveryClaim,
    WorkBuddyKnowledgeAclRow,
    WorkBuddyKnowledgeBaseRow,
    WorkBuddyKnowledgeChunkHit,
    WorkBuddyKnowledgeDocumentRow,
    WorkBuddyKnowledgeFileRefRow,
    WorkBuddyKnowledgeUploadRow,
    WorkBuddyPlatformModelRevisionRow,
    WorkBuddyTriggerDeliveryRow,
    WorkBuddyTriggerGrantRow,
    WorkBuddyTriggerRegistrationRow,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.knowledge import (
    ArchiveEntry,
    ArchiveLimits,
    EmbeddingDescriptor,
    HookRejection,
    KnowledgeHooks,
    ObjectStat,
    ObjectStoreTarget,
    ParsedDocument,
    ScanVerdict,
    TextBlock,
    TriggerHooks,
    WorkBuddyKnowledgeActor,
    WorkBuddyKnowledgeService,
    WorkBuddyTriggerService,
    compute_webhook_signature,
    configure_workbuddy_knowledge_hooks,
    require_embedder,
    require_object_store,
    reset_workbuddy_knowledge_hooks,
    resolve_knowledge_access,
    sanitize_filename,
    sha256_hex,
    timestamp_in_window,
    validate_archive_manifest,
    validate_embedding_vector,
    validate_query_text,
    verify_upload_content,
)

TENANT = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
REVISION_ID = "33333333-3333-3333-3333-333333333333"
KB_ID = "44444444-4444-4444-4444-444444444444"
DOC_ID = "55555555-5555-5555-5555-555555555555"
DOC_JOB_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
FILE_REF_ID = "66666666-6666-6666-6666-666666666666"
WEBHOOK_PATH = "abcDEF0123456789_-xyz"
SECRET = "s3cret-signing-material"


# ── doubles ──────────────────────────────────────────────────────────────────


class NoopDb:
    """Placeholder pool: the service tests never reach the database layer."""

    dialect = "postgresql"


def _base(**overrides: Any) -> WorkBuddyKnowledgeBaseRow:
    values: dict[str, Any] = {
        "kb_id": KB_ID,
        "tenant_id": TENANT,
        "scope": "personal",
        "owner_user_id": 7,
        "department_id": None,
        "name": "Base",
        "description": "",
        "embedding_model_revision_id": REVISION_ID,
        "embedding_adapter_key": "ollama",
        "embedding_model_key": "bge-m3",
        "embedding_revision": 3,
        "embedding_dimensions": EMBEDDING_DIMENSIONS,
        "archived_at": None,
        "created_by_user_id": 7,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeBaseRow(**values)


def _acl(**overrides: Any) -> WorkBuddyKnowledgeAclRow:
    values: dict[str, Any] = {
        "acl_id": "77777777-7777-7777-7777-777777777777",
        "tenant_id": TENANT,
        "kb_id": KB_ID,
        "user_id": None,
        "department_id": None,
        "permission": "read",
        "granted_by_user_id": 7,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeAclRow(**values)


def _revision(**overrides: Any) -> WorkBuddyPlatformModelRevisionRow:
    values: dict[str, Any] = {
        "model_revision_id": REVISION_ID,
        "adapter_key": "ollama",
        "model_key": "bge-m3",
        "revision": 3,
        "display_name": "bge-m3 r3",
        "status": "published",
    }
    values.update(overrides)
    return WorkBuddyPlatformModelRevisionRow(**values)


def _hit(**overrides: Any) -> WorkBuddyKnowledgeChunkHit:
    values: dict[str, Any] = {
        "chunk_id": "88888888-8888-8888-8888-888888888888",
        "document_id": DOC_ID,
        "document_title": "doc.md",
        "generation_id": "99999999-9999-9999-9999-999999999999",
        "ordinal": 0,
        "content": "hello world",
        "token_count": 2,
        "metadata": {},
        "score": 0.9,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeChunkHit(**values)


def _registration(**overrides: Any) -> WorkBuddyTriggerRegistrationRow:
    values: dict[str, Any] = {
        "registration_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "tenant_id": TENANT,
        "workflow_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "kind": "webhook",
        "name": "hook",
        "enabled": True,
        "webhook_path": WEBHOOK_PATH,
        "cron_expression": None,
        "event_name": None,
        "event_filter": {},
        "secret_provider": "VaultBackend",
        "secret_ref": "vault://workbuddy/hook",
        "secret_version": 1,
        "previous_secret_ref": None,
        "previous_secret_expires_at": None,
        "signature_algorithm": "hmac-sha256",
        "signature_header": "x-workbuddy-signature",
        "timestamp_header": "x-workbuddy-timestamp",
        "tolerance_seconds": 300,
        "created_by_user_id": 7,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
        "revoked_at": None,
    }
    values.update(overrides)
    return WorkBuddyTriggerRegistrationRow(**values)


def _delivery(**overrides: Any) -> WorkBuddyTriggerDeliveryRow:
    values: dict[str, Any] = {
        "delivery_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
        "tenant_id": TENANT,
        "registration_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "event_key": "evt-1",
        "event_name": None,
        "status": "accepted",
        "is_test": False,
        "body_sha256": sha256_hex(b"{}"),
        "signature_version": 1,
        "signature_timestamp": 1_700_000_000,
        "attempt": 1,
        "execution_id": None,
        "rejection_code": None,
        "actor_user_id": None,
        "received_at": 1_700_000_000,
        "completed_at": None,
    }
    values.update(overrides)
    return WorkBuddyTriggerDeliveryRow(**values)


def _document(**overrides: Any) -> WorkBuddyKnowledgeDocumentRow:
    values: dict[str, Any] = {
        "document_id": DOC_ID,
        "tenant_id": TENANT,
        "kb_id": KB_ID,
        "file_ref_id": FILE_REF_ID,
        "source": "upload",
        "title": "doc.md",
        "status": "pending",
        "error_code": None,
        "active_generation_id": None,
        "chunk_count": 0,
        "job_id": DOC_JOB_ID,
        "created_by_user_id": 7,
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_000,
        "deleted_at": None,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeDocumentRow(**values)


def _file_ref(**overrides: Any) -> WorkBuddyKnowledgeFileRefRow:
    values: dict[str, Any] = {
        "file_ref_id": FILE_REF_ID,
        "tenant_id": TENANT,
        "kb_id": KB_ID,
        "upload_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "object_key": "objects/a.md",
        "filename": "a.md",
        "mime_type": "text/markdown",
        "size_bytes": 11,
        "checksum_sha256": sha256_hex(b"hello world"),
        "created_by_user_id": 7,
        "created_at": 1_700_000_000,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeFileRefRow(**values)


class FakeKnowledgeRepo:
    def __init__(
        self,
        *,
        base: WorkBuddyKnowledgeBaseRow | None = None,
        acl: list[WorkBuddyKnowledgeAclRow] | None = None,
        revision: WorkBuddyPlatformModelRevisionRow | None = None,
        granted: bool = True,
        hits: list[WorkBuddyKnowledgeChunkHit] | None = None,
        document: WorkBuddyKnowledgeDocumentRow | None = None,
        file_ref: WorkBuddyKnowledgeFileRefRow | None = None,
    ) -> None:
        self.base = base if base is not None else _base()
        self.acl = acl or []
        self.revision = revision if revision is not None else _revision()
        self.granted = granted
        self.hits = hits if hits is not None else [_hit()]
        self.document = document if document is not None else _document()
        self.file_ref = file_ref if file_ref is not None else _file_ref()
        self.published: list[dict[str, Any]] = []
        self.statuses: list[tuple[str, str | None]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.created_documents: list[dict[str, Any]] = []
        self.renames: list[dict[str, Any]] = []

    # reads
    def get_base(self, ctx: Any, kb_id: str, *, include_archived: bool = True) -> Any:
        if self.base is None or self.base.kb_id != kb_id:
            return None
        if not include_archived and self.base.archived_at is not None:
            return None
        return self.base

    def list_bases(self, ctx: Any, *, limit: int = 200) -> list[WorkBuddyKnowledgeBaseRow]:
        return [self.base] if self.base is not None else []

    def effective_acl(
        self, ctx: Any, kb_id: str, *, user_id: int, department_id: str | None
    ) -> list[Any]:
        return [
            row
            for row in self.acl
            if (row.user_id is not None and row.user_id == user_id)
            or (row.department_id is not None and row.department_id == department_id)
        ]

    def effective_acl_map(
        self, ctx: Any, kb_ids: list[str], *, user_id: int, department_id: str | None
    ) -> dict[str, list[Any]]:
        if self.base is None or self.base.kb_id not in kb_ids:
            return {}
        return {
            self.base.kb_id: self.effective_acl(
                ctx, self.base.kb_id, user_id=user_id, department_id=department_id
            )
        }

    def get_platform_model_revision(self, ctx: Any, model_revision_id: str) -> Any:
        if self.revision is None or self.revision.model_revision_id != model_revision_id:
            return None
        return self.revision

    def tenant_model_granted(self, ctx: Any, model_revision_id: str) -> bool:
        return self.granted

    def search_chunks(
        self, ctx: Any, kb_id: str, *, query_vector: list[float], limit: int
    ) -> list[Any]:
        self.search_calls.append({"kb_id": kb_id, "dimensions": len(query_vector), "limit": limit})
        return self.hits[:limit]

    def get_document(self, ctx: Any, kb_id: str, document_id: str) -> Any:
        if self.document is None or self.document.kb_id != kb_id:
            return None
        return self.document if self.document.document_id == document_id else None

    def create_document(self, ctx: Any, kb_id: str, **kwargs: Any) -> Any:
        self.created_documents.append({"kb_id": kb_id, **kwargs})
        return _document(
            document_id=kwargs.get("document_id") or DOC_ID,
            kb_id=kb_id,
            file_ref_id=kwargs["file_ref_id"],
            source=kwargs.get("source", "upload"),
            title=kwargs["title"],
            job_id=kwargs["job_id"],
        )

    def get_file_ref(self, ctx: Any, kb_id: str, file_ref_id: str) -> Any:
        if self.file_ref is None or self.file_ref.kb_id != kb_id:
            return None
        return self.file_ref if self.file_ref.file_ref_id == file_ref_id else None

    def rename_document(self, ctx: Any, kb_id: str, document_id: str, *, title: str) -> bool:
        """Mirrors the row contract: ``False`` when this document is not visible."""
        self.renames.append({"kb_id": kb_id, "document_id": document_id, "title": title})
        if self.document is None or self.document.kb_id != kb_id:
            return False
        return self.document.document_id == document_id

    def department_exists(self, ctx: Any, department_id: str) -> bool:
        return department_id == "dddddddd-0000-0000-0000-000000000000"

    def tenant_member_exists(self, ctx: Any, user_id: int) -> bool:
        return user_id in {7, 8, 9}

    # writes
    def set_document_status(
        self, ctx: Any, kb_id: str, document_id: str, *, status: str, error_code: str | None = None
    ) -> None:
        self.statuses.append((status, error_code))

    def publish_generation(self, ctx: Any, **kwargs: Any) -> Any:
        self.published.append(kwargs)
        # The caller reads the published generation back (its id and chunk count),
        # exactly as the repository's row contract promises.
        return SimpleNamespace(
            generation_id=f"generation-{len(self.published)}",
            chunk_count=len(kwargs.get("chunks") or ()),
        )

    def reject_upload(self, ctx: Any, kb_id: str, upload_id: str, *, rejection_code: str) -> None:
        self.rejected = rejection_code


class FakeTriggerRepo:
    def __init__(self, registration: WorkBuddyTriggerRegistrationRow | None = None) -> None:
        self.registration = registration if registration is not None else _registration()
        self.ledger: dict[tuple[str, str, str], WorkBuddyTriggerDeliveryRow] = {}
        self.grants: list[WorkBuddyTriggerGrantRow] = []
        self.completed: list[str] = []
        self.rejected: list[str] = []

    def get_registration(self, ctx: Any, registration_id: str) -> Any:
        if self.registration is None or self.registration.registration_id != registration_id:
            return None
        return self.registration

    def resolve_webhook_registration(self, webhook_path: str) -> Any:
        if self.registration is None or self.registration.webhook_path != webhook_path:
            return None
        return self.registration

    def claim_delivery(self, ctx: Any, registration_id: str, **fields: Any) -> DeliveryClaim:
        key = (TENANT, registration_id, str(fields["event_key"]))
        existing = self.ledger.get(key)
        if existing is not None and existing.status != "failed":
            return DeliveryClaim(existing, False)
        row = _delivery(
            registration_id=registration_id,
            event_key=str(fields["event_key"]),
            body_sha256=str(fields["body_sha256"]),
            is_test=bool(fields["is_test"]),
            attempt=1 if existing is None else existing.attempt + 1,
            status="accepted",
        )
        self.ledger[key] = row
        return DeliveryClaim(row, True)

    def complete_delivery(self, ctx: Any, delivery_id: str, *, execution_id: str) -> None:
        self.completed.append(execution_id)
        for key, row in self.ledger.items():
            if row.delivery_id == delivery_id:
                self.ledger[key] = replace(row, status="executed", execution_id=execution_id)

    def reject_delivery(self, ctx: Any, delivery_id: str, *, rejection_code: str) -> None:
        self.rejected.append(rejection_code)
        for key, row in self.ledger.items():
            if row.delivery_id == delivery_id:
                self.ledger[key] = replace(row, status="failed", rejection_code=rejection_code)

    def list_grants(self, ctx: Any, registration_id: str) -> list[Any]:
        return [grant for grant in self.grants if grant.registration_id == registration_id]

    def workflow_exists(self, ctx: Any, workflow_id: str) -> bool:
        return self.registration is not None and self.registration.workflow_id == workflow_id

    def update_registration_secret(self, ctx: Any, registration_id: str, **fields: Any) -> Any:
        row = self.registration
        if row is None or row.registration_id != registration_id or row.revoked_at is not None:
            return None
        self.registration = replace(
            row,
            secret_provider=str(fields["secret_provider"]),
            secret_ref=str(fields["secret_ref"]),
            secret_version=int(fields["secret_version"]),
            previous_secret_ref=fields["previous_secret_ref"],
            previous_secret_expires_at=fields["previous_secret_expires_at"],
            updated_at=int(fields.get("updated_at", 1_700_000_000)),
        )
        return self.registration


class FakeObjectStore:
    def __init__(self, data: bytes = b"hello world", *, checksum: str | None = None) -> None:
        self.data = data
        self.checksum = checksum
        self.read_limit: int | None = None

    def available(self) -> bool:
        return True

    def allocate_target(
        self, *, tenant_id: str, kb_id: str, upload_id: str, filename: str
    ) -> ObjectStoreTarget:
        return ObjectStoreTarget(object_key=f"objects/{tenant_id}/{kb_id}/{upload_id}/{filename}")

    def stat(self, object_key: str) -> ObjectStat | None:
        return ObjectStat(size_bytes=len(self.data), checksum_sha256=self.checksum)

    def read(self, object_key: str, *, limit: int) -> bytes:
        self.read_limit = limit
        return self.data[:limit]


class FakeScanner:
    def __init__(self, status: str = "clean") -> None:
        self.status = status

    def available(self) -> bool:
        return True

    def scan(self, data: bytes, *, filename: str, mime_type: str) -> ScanVerdict:
        return ScanVerdict(self.status)


class FakeParser:
    def __init__(self, parsed: ParsedDocument | None = None, *, fail: bool = False) -> None:
        self.parsed = (
            parsed
            if parsed is not None
            else ParsedDocument(text_blocks=(TextBlock("hello knowledge world"),))
        )
        self.fail = fail

    def available(self) -> bool:
        return True

    def parse(self, data: bytes, *, filename: str, mime_type: str) -> ParsedDocument:
        if self.fail:
            raise HookRejection("NO_TEXT", "unparsable")
        return self.parsed


class FakeEmbedder:
    def __init__(
        self,
        *,
        dimensions: int = EMBEDDING_DIMENSIONS,
        zero: bool = False,
        descriptor: EmbeddingDescriptor | None = None,
    ) -> None:
        self.dimensions = dimensions
        self.zero = zero
        self.descriptor = descriptor or EmbeddingDescriptor(
            "ollama", "bge-m3", 3, EMBEDDING_DIMENSIONS
        )

    def available(self) -> bool:
        return True

    def describe(self) -> EmbeddingDescriptor | None:
        return self.descriptor

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimensions if self.zero else [0.1] * self.dimensions for _ in texts]


class FakeSecretBackend:
    def __init__(self, secrets_by_ref: dict[str, str] | None = None) -> None:
        self.store: dict[str, str] = secrets_by_ref or {}
        self.created: list[str] = []

    def available(self) -> bool:
        return True

    def create_secret(self, *, name: str, value: str) -> str:
        reference = f"vault://{name}"
        self.store[reference] = value
        self.created.append(reference)
        return reference

    def read_secret(self, reference: str) -> str | None:
        return self.store.get(reference)

    def delete_secret(self, reference: str) -> None:
        self.store.pop(reference, None)


class FakeDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    def dispatch(self, **kwargs: Any) -> str:
        self.calls.append(str(kwargs["event_key"]))
        return f"exec-{len(self.calls)}"


@pytest.fixture(autouse=True)
def _reset_hooks() -> None:
    reset_workbuddy_knowledge_hooks()
    yield
    reset_workbuddy_knowledge_hooks()


class _FakeJobs:
    """The tenant's job facts, in memory: this suite has no database."""

    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.begun: list[str] = []
        self.finished: list[tuple[str, str, dict[str, Any] | None]] = []

    def start(self, *, kind: str, request: Any = None) -> str:
        self.started.append((kind, dict(request or {})))
        return f"job-{len(self.started)}"

    def begin(self, job_id: str) -> bool:
        self.begun.append(job_id)
        return True

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        self.finished.append((job_id, status, dict(result) if result is not None else None))
        return True


def _service(
    repo: FakeKnowledgeRepo,
    hooks: KnowledgeHooks,
    *,
    jobs: _FakeJobs | None = None,
) -> WorkBuddyKnowledgeService:
    return WorkBuddyKnowledgeService(
        db=NoopDb(),
        hooks=hooks,
        repo=repo,
        clock=lambda: 1_700_000_000,
        jobs=jobs if jobs is not None else _FakeJobs(),
    )


def _trigger_service(repo: FakeTriggerRepo, hooks: TriggerHooks) -> WorkBuddyTriggerService:
    return WorkBuddyTriggerService(db=NoopDb(), hooks=hooks, repo=repo, clock=lambda: 1_700_000_000)


def _actor(**overrides: Any) -> WorkBuddyKnowledgeActor:
    values: dict[str, Any] = {
        "user_id": 7,
        "tenant_id": TENANT,
        "department_id": None,
        "is_tenant_admin": False,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeActor(**values)


# ── permission precedence ────────────────────────────────────────────────────


def test_enterprise_scope_reads_for_every_member() -> None:
    access = resolve_knowledge_access(
        _base(scope="enterprise", owner_user_id=None),
        user_id=42,
        department_id=None,
        is_tenant_admin=False,
    )
    assert access.permission == "read"
    assert access.can_read and not access.can_write


def test_department_scope_reads_only_for_current_members() -> None:
    base = _base(scope="department", owner_user_id=None, department_id="dept-a")
    member = resolve_knowledge_access(
        base, user_id=42, department_id="dept-a", is_tenant_admin=False
    )
    outsider = resolve_knowledge_access(
        base, user_id=43, department_id="dept-b", is_tenant_admin=False
    )
    assert member.permission == "read"
    assert outsider.permission is None


def test_department_admin_reads_as_admin_but_personal_owner_only() -> None:
    department = resolve_knowledge_access(
        _base(scope="department", owner_user_id=None, department_id="dept-a"),
        user_id=42,
        department_id="dept-b",
        is_tenant_admin=True,
    )
    personal = resolve_knowledge_access(
        _base(scope="personal", owner_user_id=7),
        user_id=42,
        department_id=None,
        is_tenant_admin=True,
    )
    assert department.can_admin
    # Acceptance: a tenant admin has no implicit read of another member's base.
    assert personal.permission is None


def test_personal_owner_and_additive_acl() -> None:
    base = _base(scope="personal", owner_user_id=7)
    owner = resolve_knowledge_access(base, user_id=7, department_id=None, is_tenant_admin=False)
    granted = resolve_knowledge_access(
        base,
        user_id=8,
        department_id=None,
        is_tenant_admin=False,
        acl_rows=[_acl(user_id=8, permission="write")],
    )
    department_granted = resolve_knowledge_access(
        base,
        user_id=9,
        department_id="dept-a",
        is_tenant_admin=False,
        acl_rows=[_acl(department_id="dept-a", permission="read")],
    )
    assert owner.can_admin
    assert granted.permission == "write"
    assert department_granted.permission == "read"


def test_revoked_acl_rows_deny_the_next_transaction() -> None:
    base = _base(scope="personal", owner_user_id=7)
    before = resolve_knowledge_access(
        base,
        user_id=8,
        department_id=None,
        is_tenant_admin=False,
        acl_rows=[_acl(user_id=8, permission="read")],
    )
    after = resolve_knowledge_access(
        base, user_id=8, department_id=None, is_tenant_admin=False, acl_rows=[]
    )
    assert before.can_read
    assert after.permission is None


def test_acl_never_grants_permission_below_base_read() -> None:
    access = resolve_knowledge_access(
        _base(scope="department", owner_user_id=None, department_id="dept-a"),
        user_id=8,
        department_id="dept-b",
        is_tenant_admin=False,
        acl_rows=[_acl(user_id=8, permission="admin")],
    )
    assert access.can_admin


# ── payload: why this caller reads a base, and renaming a document ───────────


def test_the_base_payload_reports_the_callers_own_access_sources() -> None:
    """The page explains and filters rows by the resolver's own answer."""
    repo = FakeKnowledgeRepo(
        base=_base(scope="personal", owner_user_id=7),
        acl=[_acl(user_id=8, permission="read")],
    )
    service = _service(repo, _search_hooks(FakeEmbedder()))

    owner = service.list_bases(_actor(user_id=7))
    granted = service.list_bases(_actor(user_id=8))
    assert owner[0]["access_sources"] == ["owner"]
    assert granted[0]["access_sources"] == ["acl:read"]
    # The payload is the resolver's list, not a second copy of the rule.
    computed = resolve_knowledge_access(
        repo.base,
        user_id=8,
        department_id=None,
        is_tenant_admin=False,
        acl_rows=repo.acl,
    )
    assert granted[0]["access_sources"] == list(computed.sources)

    enterprise = _service(
        FakeKnowledgeRepo(base=_base(scope="enterprise", owner_user_id=None)),
        _search_hooks(FakeEmbedder()),
    ).list_bases(_actor(user_id=42))
    assert enterprise[0]["access_sources"] == ["enterprise-member"]

    # Another department's base reached as tenant admin is *that* reason, not a
    # department membership the resolver never granted.
    admin_viewed = _service(
        FakeKnowledgeRepo(
            base=_base(
                scope="department",
                owner_user_id=None,
                department_id="88888888-8888-4888-8888-888888888888",
            )
        ),
        _search_hooks(FakeEmbedder()),
    ).list_bases(
        _actor(
            user_id=42,
            department_id="99999999-9999-4999-8999-999999999999",
            is_tenant_admin=True,
        )
    )
    assert admin_viewed[0]["access_sources"] == ["tenant-admin"]


def test_renaming_a_document_needs_write_permission() -> None:
    owner_repo = FakeKnowledgeRepo()
    owner = _service(owner_repo, _search_hooks(FakeEmbedder()))

    with pytest.raises(OctopError) as blank:
        owner.rename_document(_actor(), KB_ID, DOC_ID, title="   ")
    assert blank.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT

    with pytest.raises(OctopError) as too_long:
        owner.rename_document(_actor(), KB_ID, DOC_ID, title="x" * 256)
    assert too_long.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT
    assert owner_repo.renames == []

    reader = _service(
        FakeKnowledgeRepo(base=_base(scope="enterprise", owner_user_id=None)),
        _search_hooks(FakeEmbedder()),
    )
    with pytest.raises(OctopError) as forbidden:
        reader.rename_document(_actor(user_id=42), KB_ID, DOC_ID, title="名称")
    assert forbidden.value.code is ErrorCode.FORBIDDEN

    with pytest.raises(OctopError) as invisible:
        owner.rename_document(_actor(user_id=99), KB_ID, DOC_ID, title="名称")
    assert invisible.value.code is ErrorCode.NOT_FOUND


def test_renaming_stores_the_trimmed_title_for_a_writer() -> None:
    repo = FakeKnowledgeRepo()
    service = _service(repo, _search_hooks(FakeEmbedder()))

    payload = service.rename_document(_actor(), KB_ID, DOC_ID, title="  运维手册  ")
    assert payload == {"document_id": DOC_ID, "kb_id": KB_ID, "title": "运维手册"}
    assert repo.renames == [{"kb_id": KB_ID, "document_id": DOC_ID, "title": "运维手册"}]

    # A document the repository cannot see (here: another document id) stays
    # invisible instead of being reported as renamed.
    missing = _service(
        FakeKnowledgeRepo(document=_document(document_id="99999999-9999-4999-8999-999999999999")),
        _search_hooks(FakeEmbedder()),
    )
    with pytest.raises(OctopError) as not_found:
        missing.rename_document(_actor(), KB_ID, DOC_ID, title="名称")
    assert not_found.value.code is ErrorCode.NOT_FOUND


# ── bounded upload / archive validation ──────────────────────────────────────


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b.md", "..\\win.md", "C:\\x.md", "", "  "])
def test_filename_path_traversal_is_rejected(name: str) -> None:
    with pytest.raises((HookRejection, OctopError)):
        sanitize_filename(name)


def test_filename_accepts_bare_names() -> None:
    assert sanitize_filename("Quarterly Report.pdf") == "Quarterly Report.pdf"


def test_upload_content_checks_size_mime_and_magic() -> None:
    assert (
        verify_upload_content(filename="a.md", declared_mime="text/markdown", data=b"# hi")
        == "text/markdown"
    )
    with pytest.raises(OctopError) as mismatch:
        verify_upload_content(filename="a.md", declared_mime="application/pdf", data=b"# hi")
    assert mismatch.value.code is ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE
    with pytest.raises(OctopError):
        verify_upload_content(filename="a.pdf", declared_mime="application/pdf", data=b"not a pdf")
    with pytest.raises(OctopError):
        verify_upload_content(
            filename="a.md", declared_mime="text/markdown", data=b"# hi", declared_size=99
        )


def test_archive_manifest_rejects_path_traversal() -> None:
    with pytest.raises(HookRejection) as excinfo:
        validate_archive_manifest(
            [ArchiveEntry(name="../../etc/passwd", uncompressed_bytes=10, compressed_bytes=10)]
        )
    assert excinfo.value.code == "ARCHIVE_PATH_TRAVERSAL"
    with pytest.raises(HookRejection):
        validate_archive_manifest(
            [ArchiveEntry(name="/absolute.md", uncompressed_bytes=10, compressed_bytes=10)]
        )


def test_archive_manifest_rejects_zip_bombs() -> None:
    with pytest.raises(HookRejection) as ratio:
        validate_archive_manifest(
            [ArchiveEntry(name="bomb.txt", uncompressed_bytes=200_000_000, compressed_bytes=1_000)]
        )
    assert ratio.value.code == "ARCHIVE_BOMB"
    with pytest.raises(HookRejection):
        validate_archive_manifest(
            [
                ArchiveEntry(name=f"f{i}.txt", uncompressed_bytes=1_000, compressed_bytes=1_000)
                for i in range(ArchiveLimits().max_entries + 1)
            ]
        )
    validate_archive_manifest(
        [ArchiveEntry(name="ok.txt", uncompressed_bytes=1_000, compressed_bytes=1_000)]
    )


# ── query / vector / hook fail-closed ───────────────────────────────────────


def test_query_and_vector_validation() -> None:
    assert validate_query_text("  hello  ") == "hello"
    with pytest.raises(OctopError):
        validate_query_text("   ")
    vector = validate_embedding_vector([0.5] * EMBEDDING_DIMENSIONS)
    assert len(vector) == EMBEDDING_DIMENSIONS
    with pytest.raises(OctopError):
        validate_embedding_vector([0.5] * (EMBEDDING_DIMENSIONS - 1))
    with pytest.raises(OctopError):
        validate_embedding_vector([0.0] * EMBEDDING_DIMENSIONS)
    with pytest.raises(OctopError):
        validate_embedding_vector([float("nan")] * EMBEDDING_DIMENSIONS)


def test_missing_hooks_fail_closed() -> None:
    with pytest.raises(OctopError) as storage:
        require_object_store(None)
    assert storage.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    with pytest.raises(OctopError) as embedder:
        require_embedder(None)
    assert embedder.value.code is ErrorCode.MODEL_NOT_CONFIGURED


def test_registry_is_process_wide_and_resettable() -> None:
    hooks = KnowledgeHooks(object_store=FakeObjectStore())
    configure_workbuddy_knowledge_hooks(hooks)
    assert require_object_store(hooks.object_store).available() is True
    reset_workbuddy_knowledge_hooks()
    with pytest.raises(OctopError):
        require_object_store(None)


# ── search ───────────────────────────────────────────────────────────────────


def _search_hooks(embedder: Any) -> KnowledgeHooks:
    return KnowledgeHooks(embedder=embedder)


def test_search_requires_the_pinned_model_and_revision() -> None:
    repo = FakeKnowledgeRepo()
    actor = _actor()
    service = _service(repo, _search_hooks(FakeEmbedder()))
    payload = service.search(actor, KB_ID, query="hello", match_count=3)
    assert repo.search_calls == [{"kb_id": KB_ID, "dimensions": EMBEDDING_DIMENSIONS, "limit": 3}]
    assert payload["hits"][0]["chunk_id"] == _hit().chunk_id
    assert payload["embedding"]["model_key"] == "bge-m3"
    assert payload["embedding"]["revision"] == 3

    drift = _service(
        FakeKnowledgeRepo(),
        _search_hooks(
            FakeEmbedder(
                descriptor=EmbeddingDescriptor("ollama", "bge-m3", 4, EMBEDDING_DIMENSIONS)
            )
        ),
    )
    with pytest.raises(OctopError) as excinfo:
        drift.search(_actor(), KB_ID, query="hello")
    assert excinfo.value.code is ErrorCode.MODEL_NOT_CONFIGURED


def test_search_rejects_other_models_and_bad_vectors() -> None:
    service = _service(FakeKnowledgeRepo(), _search_hooks(FakeEmbedder()))
    with pytest.raises(OctopError) as model:
        service.search(_actor(), KB_ID, query="hello", embedding_model="other-model")
    assert model.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT
    with pytest.raises(OctopError):
        service.search(_actor(), KB_ID, query="hello", match_count=500)

    zero = _service(FakeKnowledgeRepo(), _search_hooks(FakeEmbedder(zero=True)))
    with pytest.raises(OctopError) as excinfo:
        zero.search(_actor(), KB_ID, query="hello")
    assert excinfo.value.code is ErrorCode.MODEL_NOT_CONFIGURED

    wrong_width = _service(
        FakeKnowledgeRepo(),
        _search_hooks(
            FakeEmbedder(dimensions=768, descriptor=EmbeddingDescriptor("ollama", "bge-m3", 3, 768))
        ),
    )
    with pytest.raises(OctopError):
        wrong_width.search(_actor(), KB_ID, query="hello")


def test_search_fails_closed_without_grant_or_embedder() -> None:
    revoked = _service(
        FakeKnowledgeRepo(revision=_revision(status="revoked")), _search_hooks(FakeEmbedder())
    )
    with pytest.raises(OctopError) as excinfo:
        revoked.search(_actor(), KB_ID, query="hello")
    assert excinfo.value.code is ErrorCode.WORKBUDDY_PLATFORM_REVISION_REVOKED

    ungranted = _service(FakeKnowledgeRepo(granted=False), _search_hooks(FakeEmbedder()))
    with pytest.raises(OctopError) as grant:
        ungranted.search(_actor(), KB_ID, query="hello")
    assert grant.value.code is ErrorCode.WORKBUDDY_CAPABILITY_NOT_APPROVED


def test_search_is_impossible_for_invisible_bases() -> None:
    service = _service(FakeKnowledgeRepo(), _search_hooks(FakeEmbedder()))
    with pytest.raises(OctopError) as excinfo:
        service.search(_actor(user_id=99), KB_ID, query="hello")
    assert excinfo.value.code is ErrorCode.NOT_FOUND
    # A visible-but-read-only member cannot write.
    enterprise = _service(
        FakeKnowledgeRepo(base=_base(scope="enterprise", owner_user_id=None)),
        _search_hooks(FakeEmbedder()),
    )
    with pytest.raises(OctopError) as write:
        enterprise.create_upload(
            _actor(user_id=42),
            KB_ID,
            filename="a.md",
            mime_type="text/markdown",
            size_bytes=10,
        )
    assert write.value.code is ErrorCode.FORBIDDEN


# ── indexing ─────────────────────────────────────────────────────────────────


def _index_hooks(
    *,
    store: Any = None,
    parser: Any = None,
    embedder: Any = None,
) -> KnowledgeHooks:
    return KnowledgeHooks(
        object_store=store,
        parser=parser or FakeParser(),
        embedder=embedder or FakeEmbedder(),
    )


def test_indexing_publishes_one_ready_generation_atomically() -> None:
    repo = FakeKnowledgeRepo()
    jobs = _FakeJobs()
    service = _service(repo, _index_hooks(store=FakeObjectStore(b"hello world")), jobs=jobs)
    service.index_document(tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID)
    assert len(repo.published) == 1
    published = repo.published[0]
    assert published["document_id"] == DOC_ID
    assert all(len(chunk[4]) == EMBEDDING_DIMENSIONS for chunk in published["chunks"])
    assert repo.statuses == [("parsing", None), ("indexing", None)]
    # The document's job reports the work: running while it happens, and the
    # published generation once it is done.
    assert jobs.begun == [DOC_JOB_ID], jobs.begun
    assert jobs.finished == [
        (
            DOC_JOB_ID,
            "succeeded",
            {
                "document_id": DOC_ID,
                "kb_id": KB_ID,
                "generation_id": "generation-1",
                "chunk_count": len(published["chunks"]),
            },
        )
    ], jobs.finished


def test_indexing_fails_closed_without_publishing() -> None:
    parser_offline = _service(
        FakeKnowledgeRepo(),
        KnowledgeHooks(embedder=FakeEmbedder(), parser=None, object_store=FakeObjectStore()),
    )
    with pytest.raises(OctopError):
        parser_offline.index_document(
            tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID
        )
    assert parser_offline.repo is not None and parser_offline.repo.published == []
    assert parser_offline.repo.statuses[-1][0] == "failed"

    bomb = _service(
        FakeKnowledgeRepo(),
        _index_hooks(
            store=FakeObjectStore(b"hello world"),
            parser=FakeParser(
                ParsedDocument(
                    text_blocks=(TextBlock("bomb"),),
                    archive_entries=(
                        ArchiveEntry(
                            name="../../escape.txt",
                            uncompressed_bytes=10,
                            compressed_bytes=10,
                        ),
                    ),
                )
            ),
        ),
    )
    bomb_jobs = _FakeJobs()
    with pytest.raises(HookRejection):
        # ``bomb`` already carries its hooks; only the job recorder is swapped so
        # the failure can be read back off the job.
        bomb.jobs = bomb_jobs
        bomb.index_document(tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID)
    assert bomb.repo is not None and bomb.repo.published == []
    assert bomb_jobs.begun == [DOC_JOB_ID], bomb_jobs.begun
    assert bomb_jobs.finished and bomb_jobs.finished[0][1] == "failed", bomb_jobs.finished

    tampered = _service(
        FakeKnowledgeRepo(),
        _index_hooks(store=FakeObjectStore(b"tampered body")),
    )
    with pytest.raises(HookRejection):
        tampered.index_document(tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID)
    assert tampered.repo is not None and tampered.repo.published == []


def test_indexing_never_crosses_knowledge_bases() -> None:
    service = _service(FakeKnowledgeRepo(), _index_hooks(store=FakeObjectStore(b"hello world")))
    with pytest.raises(OctopError) as excinfo:
        service.index_document(
            tenant_id=TENANT,
            actor_user_id=7,
            kb_id="00000000-0000-0000-0000-000000000000",
            document_id=DOC_ID,
        )
    assert excinfo.value.code is ErrorCode.NOT_FOUND


# ── upload completion ────────────────────────────────────────────────────────


def _upload_row(**overrides: Any) -> WorkBuddyKnowledgeUploadRow:
    values: dict[str, Any] = {
        "upload_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "tenant_id": TENANT,
        "kb_id": KB_ID,
        "requested_by_user_id": 7,
        "filename": "a.md",
        "mime_type": "text/markdown",
        "size_bytes": 11,
        "object_key": "objects/a.md",
        "status": "pending",
        "checksum_sha256": None,
        "detected_mime": None,
        "scan_status": None,
        "file_ref_id": None,
        "rejection_code": None,
        "created_at": 1_700_000_000,
        "expires_at": 1_700_000_900,
        "completed_at": None,
    }
    values.update(overrides)
    return WorkBuddyKnowledgeUploadRow(**values)


def test_upload_completion_rejects_hash_and_scanner_failures() -> None:
    class UploadRepo(FakeKnowledgeRepo):
        def __init__(self, upload: WorkBuddyKnowledgeUploadRow) -> None:
            super().__init__()
            self.upload = upload
            self.completed: list[dict[str, Any]] = []

        def get_upload(self, ctx: Any, kb_id: str, upload_id: str) -> Any:
            return self.upload

        def complete_upload(self, ctx: Any, upload: Any, **fields: Any) -> Any:
            self.completed.append(fields)
            return _file_ref(kb_id=upload.kb_id)

    upload = _upload_row()
    good = UploadRepo(upload)
    service = _service(
        good,
        KnowledgeHooks(
            object_store=FakeObjectStore(b"hello world"),
            scanner=FakeScanner(),
            embedder=FakeEmbedder(),
        ),
    )
    ref = service.complete_upload(_actor(), KB_ID, upload.upload_id)
    assert ref["checksum_sha256"] == sha256_hex(b"hello world")
    assert good.completed

    infected = UploadRepo(upload)
    scanner = _service(
        infected,
        KnowledgeHooks(
            object_store=FakeObjectStore(b"hello world"),
            scanner=FakeScanner("infected"),
            embedder=FakeEmbedder(),
        ),
    )
    with pytest.raises(OctopError):
        scanner.complete_upload(_actor(), KB_ID, upload.upload_id)
    assert infected.completed == []

    tampered = UploadRepo(upload)
    checksum = _service(
        tampered,
        KnowledgeHooks(
            object_store=FakeObjectStore(b"hello world", checksum="0" * 64),
            scanner=FakeScanner(),
            embedder=FakeEmbedder(),
        ),
    )
    with pytest.raises(OctopError):
        checksum.complete_upload(_actor(), KB_ID, upload.upload_id)
    assert tampered.completed == []

    fake_pdf = b"GIF89a-not-a-pdf"
    pdf_upload = _upload_row(
        filename="a.pdf", mime_type="application/pdf", size_bytes=len(fake_pdf)
    )
    wrong_magic = UploadRepo(pdf_upload)
    magic = _service(
        wrong_magic,
        KnowledgeHooks(
            object_store=FakeObjectStore(fake_pdf),
            scanner=FakeScanner(),
            embedder=FakeEmbedder(),
        ),
    )
    with pytest.raises(OctopError) as excinfo:
        magic.complete_upload(_actor(), KB_ID, pdf_upload.upload_id)
    assert excinfo.value.code is ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE
    assert wrong_magic.completed == []

    binary_text = b"# title\x00binary"
    md_upload = _upload_row(size_bytes=len(binary_text))
    disguised = UploadRepo(md_upload)
    content_sniff = _service(
        disguised,
        KnowledgeHooks(
            object_store=FakeObjectStore(binary_text),
            scanner=FakeScanner(),
            embedder=FakeEmbedder(),
        ),
    )
    with pytest.raises(OctopError) as sniffed:
        content_sniff.complete_upload(_actor(), KB_ID, md_upload.upload_id)
    assert sniffed.value.code is ErrorCode.KNOWLEDGE_UNSUPPORTED_TYPE
    assert disguised.completed == []


# ── webhook ingest ───────────────────────────────────────────────────────────


def _webhook_hooks(backend: Any, dispatcher: Any) -> TriggerHooks:
    return TriggerHooks(secret_backend=backend, dispatcher=dispatcher)


def _signed(body: bytes, *, timestamp: int = 1_700_000_000, secret: str = SECRET) -> dict[str, str]:
    return {
        "x-workbuddy-timestamp": str(timestamp),
        "x-workbuddy-signature": compute_webhook_signature(
            secret=secret, timestamp=timestamp, body=body
        ),
        "x-workbuddy-event-id": "evt-1",
    }


def test_webhook_replay_never_executes_twice() -> None:
    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    dispatcher = FakeDispatcher()
    repo = FakeTriggerRepo()
    service = _trigger_service(repo, _webhook_hooks(backend, dispatcher))
    body = b'{"event": "wf.started"}'
    first = service.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body))
    second = service.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body))
    assert first["status"] == "executed" and first["duplicate"] is False
    assert second["duplicate"] is True and second["execution_id"] == first["execution_id"]
    assert len(dispatcher.calls) == 1


def test_webhook_signature_window_and_tampering() -> None:
    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    dispatcher = FakeDispatcher()
    service = _trigger_service(FakeTriggerRepo(), _webhook_hooks(backend, dispatcher))
    body = b'{"event": "wf.started"}'
    with pytest.raises(OctopError) as tampered:
        service.ingest_webhook(
            WEBHOOK_PATH, raw_body=b'{"event": "wf.deleted"}', headers=_signed(body)
        )
    assert tampered.value.code is ErrorCode.TRIGGER_SIGNATURE_INVALID
    with pytest.raises(OctopError) as expired:
        service.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body, timestamp=1_000))
    assert expired.value.code is ErrorCode.TRIGGER_SIGNATURE_INVALID
    assert dispatcher.calls == []
    assert timestamp_in_window(1_700_000_000, tolerance_seconds=300, now=1_700_000_100)
    assert not timestamp_in_window(1_700_000_000, tolerance_seconds=300, now=1_700_001_000)


def test_webhook_fails_closed_without_proven_dependencies() -> None:
    body = b'{"event": "wf.started"}'
    no_backend = _trigger_service(FakeTriggerRepo(), _webhook_hooks(None, FakeDispatcher()))
    with pytest.raises(OctopError) as backend:
        no_backend.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body))
    assert backend.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE

    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    repo = FakeTriggerRepo()
    no_dispatcher = _trigger_service(repo, _webhook_hooks(backend, None))
    with pytest.raises(OctopError) as dispatcher:
        no_dispatcher.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body))
    assert dispatcher.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
    assert repo.rejected == ["DISPATCHER_UNAVAILABLE"]


def test_unknown_webhook_path_is_invisible() -> None:
    service = _trigger_service(
        FakeTriggerRepo(), _webhook_hooks(FakeSecretBackend(), FakeDispatcher())
    )
    with pytest.raises(OctopError) as excinfo:
        service.ingest_webhook("unknown-path-1234567890", raw_body=b"{}", headers={})
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_event_filter_mismatch_is_recorded_without_dispatch() -> None:
    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    dispatcher = FakeDispatcher()
    repo = FakeTriggerRepo(_registration(event_filter={"kind": "started"}))
    service = _trigger_service(repo, _webhook_hooks(backend, dispatcher))
    body = b'{"kind": "ignored"}'
    result = service.ingest_webhook(WEBHOOK_PATH, raw_body=body, headers=_signed(body))
    assert result["status"] == "rejected"
    assert dispatcher.calls == []


def test_registration_metadata_never_exposes_the_secret() -> None:
    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    repo = FakeTriggerRepo()
    service = _trigger_service(repo, _webhook_hooks(backend, FakeDispatcher()))
    view = service._registration_view(service.context(_actor()), repo.registration)
    assert view["has_secret"] is True
    assert "secret" not in view
    assert "secret_ref" not in view
    assert SECRET not in str(view)


def test_rotation_requires_a_proven_secret_backend() -> None:
    repo = FakeTriggerRepo()
    service = _trigger_service(repo, _webhook_hooks(None, FakeDispatcher()))
    with pytest.raises(OctopError) as excinfo:
        service.rotate_secret(
            _actor(), repo.registration.workflow_id, repo.registration.registration_id
        )
    assert excinfo.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE

    backend = FakeSecretBackend({"vault://workbuddy/hook": SECRET})
    proven = _trigger_service(FakeTriggerRepo(), _webhook_hooks(backend, FakeDispatcher()))
    rotated = proven.rotate_secret(
        _actor(), repo.registration.workflow_id, repo.registration.registration_id
    )
    assert rotated["secret"] and rotated["secret_version"] == 2
    assert rotated["overlap_expires_at"] == 1_700_000_300


def test_creating_a_document_opens_the_indexing_job_it_points_at() -> None:
    """A document's job id must name a job, not just look like one."""
    repo = FakeKnowledgeRepo()
    jobs = _FakeJobs()
    service = _service(repo, _index_hooks(store=FakeObjectStore(b"hello world")), jobs=jobs)

    created = service.create_document(_actor(), KB_ID, file_ref_id=FILE_REF_ID, title="doc.md")

    assert jobs.started == [
        (
            "knowledge_index",
            {
                "kb_id": KB_ID,
                "source": "upload",
                "file_ref_id": FILE_REF_ID,
                "title": "doc.md",
            },
        )
    ], jobs.started
    assert created["job_id"] == "job-1", created
    assert repo.created_documents[0]["job_id"] == "job-1", repo.created_documents


# ── text-only documents (the migration channel) ──────────────────────────────


class _ExplodingStore:
    """A store that fails the test if a text-only document is read from storage."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def available(self) -> bool:
        return True

    def read(self, object_key: str, *, limit: int) -> bytes:  # pragma: no cover - asserted
        self.reads.append(object_key)
        raise AssertionError("a text-only document must not read the object store")


def _text_only_document(**overrides: Any) -> WorkBuddyKnowledgeDocumentRow:
    return _document(file_ref_id=None, source="migration", **overrides)


def test_a_text_only_document_is_created_without_a_file_reference() -> None:
    repo = FakeKnowledgeRepo()
    jobs = _FakeJobs()
    service = _service(repo, _index_hooks(store=FakeObjectStore()), jobs=jobs)

    payload = service.create_document(_actor(), KB_ID, title="migrated policy", source="migration")

    assert payload["source"] == "migration"
    assert payload["file_ref_id"] is None
    created = repo.created_documents[0]
    assert created["source"] == "migration"
    assert created["file_ref_id"] is None
    # The job names the source and carries no file reference to read.
    kind, request = jobs.started[0]
    assert kind == "knowledge_index"
    assert request["source"] == "migration"
    assert "file_ref_id" not in request


def test_a_text_only_document_cannot_name_a_file_reference() -> None:
    service = _service(FakeKnowledgeRepo(), _index_hooks(store=FakeObjectStore()))
    with pytest.raises(OctopError) as named:
        service.create_document(
            _actor(), KB_ID, title="x", source="migration", file_ref_id=FILE_REF_ID
        )
    assert named.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT

    with pytest.raises(OctopError) as uploaded:
        service.create_document(_actor(), KB_ID, title="x", source="upload")
    assert uploaded.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT


def test_an_unknown_document_source_is_refused() -> None:
    service = _service(FakeKnowledgeRepo(), _index_hooks(store=FakeObjectStore()))
    with pytest.raises(OctopError) as excinfo:
        service.create_document(_actor(), KB_ID, title="x", source="telepathy")
    assert excinfo.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT


def test_indexing_a_text_document_never_touches_the_object_store() -> None:
    """The channel exists because a migrated document has no stored file at all."""
    store = _ExplodingStore()
    repo = FakeKnowledgeRepo(document=_text_only_document(title="migrated policy"))
    jobs = _FakeJobs()
    service = _service(repo, _index_hooks(store=store), jobs=jobs)

    service.index_text_document(
        tenant_id=TENANT,
        actor_user_id=7,
        kb_id=KB_ID,
        document_id=DOC_ID,
        text="first paragraph about policy\n\nsecond paragraph about scope",
    )

    assert store.reads == []
    published = repo.published[0]
    assert published["document_id"] == DOC_ID
    assert published["chunks"], "the supplied text must be chunked and published"
    assert all(chunk[3]["source"] == "migrated policy" for chunk in published["chunks"])
    assert all(len(chunk[4]) == EMBEDDING_DIMENSIONS for chunk in published["chunks"])
    assert repo.statuses == [("parsing", None), ("indexing", None)]
    assert jobs.begun == [DOC_JOB_ID], jobs.begun
    assert jobs.finished[-1][1] == "succeeded"


def test_the_wrong_indexing_entry_point_is_refused() -> None:
    uploaded = _service(FakeKnowledgeRepo(), _index_hooks(store=_ExplodingStore()))
    with pytest.raises(OctopError) as from_text:
        uploaded.index_text_document(
            tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID, text="body"
        )
    assert from_text.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT

    text_only = _service(
        FakeKnowledgeRepo(document=_text_only_document()),
        _index_hooks(store=_ExplodingStore()),
    )
    with pytest.raises(OctopError) as from_storage:
        text_only.index_document(tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID)
    assert from_storage.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT


def test_an_empty_text_document_fails_closed_and_records_the_failure() -> None:
    repo = FakeKnowledgeRepo(document=_text_only_document())
    jobs = _FakeJobs()
    service = _service(repo, _index_hooks(store=_ExplodingStore()), jobs=jobs)

    with pytest.raises(OctopError) as excinfo:
        service.index_text_document(
            tenant_id=TENANT, actor_user_id=7, kb_id=KB_ID, document_id=DOC_ID, text="   \n"
        )

    assert excinfo.value.code is ErrorCode.WORKBUDDY_INVALID_ARGUMENT
    assert repo.published == []
    assert repo.statuses[-1][0] == "failed"
    assert jobs.finished[-1][1] == "failed"

"""WorkBuddy knowledge base and trigger HTTP surface.

Mounted by the application factory at ``/api/v1``; every path below is relative
to that prefix and mirrors ``contracts/route-manifest.json``:

* ``/knowledge-bases`` … ``/knowledge-bases/{id}/search`` — scoped retrieval;
* ``/workflows/{id}/trigger-registrations`` and ``/triggers/{registration_id}``
  — trigger management for the workflow manager (tenant admin);
* ``/webhooks/{webhook_path}`` — the only unauthenticated route: it is
  authenticated by the raw-body HMAC and resolved through the globally unique
  webhook path, never through a client-supplied tenant.

Success bodies use :func:`workbuddy_envelope`; failures raise ``OctopError`` and
are rendered by the shared handler. Cross-tenant or invisible objects always
produce ``NOT_FOUND``, and no response ever contains a signing secret, a hash or
an internal encrypted payload.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPrincipal,
    require_workbuddy_duty,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.pool import DatabasePool
from octop.infra.db.workbuddy_context import WorkBuddyPostgresRequiredError, require_postgres
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.duties import DUTY_KB_ADMIN
from octop.infra.workbuddy.knowledge import (
    DEFAULT_TOLERANCE_SECONDS,
    DOCUMENT_SOURCE_UPLOAD,
    WorkBuddyKnowledgeActor,
    WorkBuddyKnowledgeService,
    WorkBuddyTriggerService,
    validate_document_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workbuddy"])


# ── request bodies ───────────────────────────────────────────────────────────


class KnowledgeBaseCreateBody(BaseModel):
    scope: Literal["personal", "department", "enterprise"] = Field(
        description="personal (owner only), department (current members) or enterprise (all members)."
    )
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    department_id: str | None = Field(default=None, description="Required for department scope.")
    model_revision_id: str = Field(
        description="Published bge-m3 platform model revision granted to this tenant."
    )


class AclCreateBody(BaseModel):
    permission: Literal["read", "write", "admin"]
    user_id: int | None = Field(default=None, description="Exactly one of user_id/department_id.")
    department_id: str | None = None


class AclUpdateBody(BaseModel):
    permission: Literal["read", "write", "admin"]


class UploadCreateBody(BaseModel):
    filename: str = Field(description="Bare file name; paths are rejected.")
    mime_type: str = Field(description="Must match the file extension and magic bytes.")
    size_bytes: int = Field(gt=0)


class UploadCompleteBody(BaseModel):
    checksum_sha256: str | None = Field(
        default=None, description="Optional caller-computed digest of the uploaded object."
    )


class DocumentCreateBody(BaseModel):
    upload_id: str | None = None
    file_ref: str | None = Field(
        default=None, description="File reference returned by upload completion."
    )
    file_ref_id: str | None = Field(default=None, description="Alias of file_ref.")
    title: str = ""
    source: str = Field(
        default=DOCUMENT_SOURCE_UPLOAD,
        description=(
            "Where the content comes from: 'upload' (a stored file reference), "
            "'text' (the text supplied here) or 'migration' (imported from the "
            "personal edition, text only). The text-only sources must not name a "
            "file reference."
        ),
    )
    text: str | None = Field(
        default=None,
        description="Content of a text-only document; ignored for uploads.",
    )


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    match_count: int = Field(default=5, ge=1, le=50)
    embedding_model: str | None = Field(
        default=None, description="Must equal the base's pinned model when supplied."
    )


class KnowledgeGrantBody(BaseModel):
    kb_id: str
    permission: Literal["read", "write"]


class TriggerRegistrationCreateBody(BaseModel):
    kind: Literal["cron", "webhook", "event"]
    name: str = Field(min_length=1, max_length=120)
    cron_expression: str | None = None
    event_name: str | None = None
    event_filter: dict[str, Any] | None = Field(
        default=None, description="Top-level equality constraints on the event body."
    )
    tool_grants: list[str] = Field(default_factory=list)
    kb_grants: list[KnowledgeGrantBody] = Field(default_factory=list)
    tolerance_seconds: int = Field(default=DEFAULT_TOLERANCE_SECONDS, ge=30, le=3600)
    signature_header: str = "x-workbuddy-signature"
    timestamp_header: str = "x-workbuddy-timestamp"


# ── helpers ──────────────────────────────────────────────────────────────────


def _require_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, f"{field} must be a UUID") from None


def _pool(server: Any) -> DatabasePool:
    """Return the PostgreSQL pool or fail closed for SQLite installations."""
    pool: DatabasePool = server.services.db
    try:
        require_postgres(pool)
    except WorkBuddyPostgresRequiredError as exc:
        raise OctopError(ErrorCode.WORKBUDDY_POSTGRES_REQUIRED, exc.message) from exc
    return pool


def _actor(principal: WorkBuddyPrincipal) -> WorkBuddyKnowledgeActor:
    return WorkBuddyKnowledgeActor(
        user_id=principal.user_id,
        tenant_id=principal.tenant_id,
        department_id=principal.department_id,
        is_tenant_admin=principal.is_admin,
    )


def _knowledge(server: Any) -> WorkBuddyKnowledgeService:
    return WorkBuddyKnowledgeService(_pool(server))


def _triggers(server: Any) -> WorkBuddyTriggerService:
    return WorkBuddyTriggerService(_pool(server))


def _log_index_failure(future: Any) -> None:
    try:
        future.result()
    except Exception:  # the document row already records the failure code
        logger.warning("workbuddy knowledge indexing job failed", exc_info=True)


def _schedule_indexing(
    service: WorkBuddyKnowledgeService,
    actor: WorkBuddyKnowledgeActor,
    kb_id: str,
    document_id: str,
) -> None:
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None,
        functools.partial(
            service.index_document,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            department_id=actor.department_id,
            kb_id=kb_id,
            document_id=document_id,
        ),
    )
    future.add_done_callback(_log_index_failure)


def _schedule_text_indexing(
    service: WorkBuddyKnowledgeService,
    actor: WorkBuddyKnowledgeActor,
    kb_id: str,
    document_id: str,
    text: str,
) -> None:
    """Index a text-only document off the request thread, content held in memory.

    The text travels with the task and is never written to the job record: only
    the chunks and their vectors reach the database.
    """
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(
        None,
        functools.partial(
            service.index_text_document,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            department_id=actor.department_id,
            kb_id=kb_id,
            document_id=document_id,
            text=text,
        ),
    )
    future.add_done_callback(_log_index_failure)


# ── knowledge bases ──────────────────────────────────────────────────────────


@router.get("/knowledge-bases", summary="List knowledge bases the caller may read")
async def list_knowledge_bases(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    items = _knowledge(server).list_bases(_actor(principal))
    return workbuddy_envelope(request, {"items": items, "count": len(items)})


@router.post("/knowledge-bases", status_code=201, summary="Create a knowledge base")
async def create_knowledge_base(
    request: Request,
    body: KnowledgeBaseCreateBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).create_base(
        _actor(principal),
        scope=body.scope,
        name=body.name,
        description=body.description,
        department_id=(
            _require_uuid(body.department_id, "department_id") if body.department_id else None
        ),
        model_revision_id=_require_uuid(body.model_revision_id, "model_revision_id"),
    )
    return workbuddy_envelope(request, payload)


@router.get("/knowledge-bases/{id}", summary="Read one knowledge base")
async def get_knowledge_base(
    request: Request,
    id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).get_base(_actor(principal), _require_uuid(id, "knowledge base id"))
    return workbuddy_envelope(request, payload)


@router.post(
    "/knowledge-bases/{id}/archive",
    summary="Archive a knowledge base and remove it from retrieval immediately",
)
async def archive_knowledge_base(
    request: Request,
    id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).archive_base(
        _actor(principal), _require_uuid(id, "knowledge base id")
    )
    return workbuddy_envelope(request, payload)


# ── ACL ──────────────────────────────────────────────────────────────────────


@router.get("/knowledge-bases/{id}/acl", summary="List explicit grants (base admins only)")
async def list_knowledge_acl(
    request: Request,
    id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    items = _knowledge(server).list_acl(_actor(principal), _require_uuid(id, "knowledge base id"))
    return workbuddy_envelope(request, {"items": items, "count": len(items)})


@router.post("/knowledge-bases/{id}/acl", status_code=201, summary="Add an explicit grant")
async def add_knowledge_acl(
    request: Request,
    id: str,
    body: AclCreateBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).add_acl(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        permission=body.permission,
        user_id=body.user_id,
        department_id=(
            _require_uuid(body.department_id, "department_id") if body.department_id else None
        ),
    )
    return workbuddy_envelope(request, payload)


@router.put("/knowledge-bases/{id}/acl/{acl_id}", summary="Change one grant's permission level")
async def update_knowledge_acl(
    request: Request,
    id: str,
    acl_id: str,
    body: AclUpdateBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).update_acl(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        _require_uuid(acl_id, "grant id"),
        permission=body.permission,
    )
    return workbuddy_envelope(request, payload)


@router.delete(
    "/knowledge-bases/{id}/acl/{acl_id}",
    summary="Revoke a grant (effective on the next retrieval transaction)",
)
async def delete_knowledge_acl(
    request: Request,
    id: str,
    acl_id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).delete_acl(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        _require_uuid(acl_id, "grant id"),
    )
    return workbuddy_envelope(request, payload)


# ── bound uploads ────────────────────────────────────────────────────────────


@router.post(
    "/knowledge-bases/{id}/uploads",
    status_code=201,
    summary="Request a bounded upload target for a knowledge base",
)
async def create_knowledge_upload(
    request: Request,
    id: str,
    body: UploadCreateBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).create_upload(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        filename=body.filename,
        mime_type=body.mime_type,
        size_bytes=body.size_bytes,
    )
    return workbuddy_envelope(request, payload)


@router.post(
    "/knowledge-bases/{id}/uploads/{upload_id}/complete",
    summary="Verify the uploaded object and return its bound file reference",
)
async def complete_knowledge_upload(
    request: Request,
    id: str,
    upload_id: str,
    body: UploadCompleteBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).complete_upload(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        _require_uuid(upload_id, "upload id"),
        checksum_sha256=body.checksum_sha256,
    )
    return workbuddy_envelope(request, payload)


# ── documents and search ─────────────────────────────────────────────────────


@router.post(
    "/knowledge-bases/{id}/documents",
    status_code=202,
    summary="Create a document and schedule bounded parsing/indexing",
)
async def create_knowledge_document(
    request: Request,
    id: str,
    body: DocumentCreateBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    actor = _actor(principal)
    service = _knowledge(server)
    kb_id = _require_uuid(id, "knowledge base id")
    source = str(body.source or DOCUMENT_SOURCE_UPLOAD).strip().lower()
    text_only = source != DOCUMENT_SOURCE_UPLOAD
    file_ref = body.file_ref or body.file_ref_id
    if text_only:
        if file_ref or body.upload_id:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "a text-only document cannot name a file reference",
            )
        # The content is validated here, before a document row exists, so a caller
        # cannot leave a pending document that nobody can ever index.
        validate_document_text(body.text)
    elif not file_ref:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "file_ref is required")
    payload = service.create_document(
        actor,
        kb_id,
        source=source,
        file_ref_id=_require_uuid(file_ref, "file_ref") if file_ref else None,
        upload_id=_require_uuid(body.upload_id, "upload id") if body.upload_id else None,
        title=body.title or "",
    )
    if text_only:
        _schedule_text_indexing(service, actor, kb_id, payload["document_id"], str(body.text))
    else:
        _schedule_indexing(service, actor, kb_id, payload["document_id"])
    return workbuddy_envelope(request, payload)


@router.get(
    "/knowledge-bases/{id}/documents",
    summary="List document status for a knowledge base",
)
async def list_knowledge_documents(
    request: Request,
    id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    items = _knowledge(server).list_documents(
        _actor(principal), _require_uuid(id, "knowledge base id")
    )
    return workbuddy_envelope(request, {"items": items, "count": len(items)})


@router.delete(
    "/knowledge-bases/{id}/documents/{document_id}",
    summary="Remove a document and its retrievability",
)
async def delete_knowledge_document(
    request: Request,
    id: str,
    document_id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).delete_document(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        _require_uuid(document_id, "document id"),
    )
    return workbuddy_envelope(request, payload)


@router.post(
    "/knowledge-bases/{id}/search",
    summary="Search one knowledge base with its pinned embedding revision",
)
async def search_knowledge_base(
    request: Request,
    id: str,
    body: SearchBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _knowledge(server).search(
        _actor(principal),
        _require_uuid(id, "knowledge base id"),
        query=body.query,
        match_count=body.match_count,
        embedding_model=body.embedding_model,
    )
    return workbuddy_envelope(request, payload)


# ── trigger registrations (workflow manager) ─────────────────────────────────


@router.post(
    "/workflows/{id}/trigger-registrations",
    status_code=201,
    summary="Register a cron, webhook or event trigger for a workflow",
)
async def create_trigger_registration(
    request: Request,
    id: str,
    body: TriggerRegistrationCreateBody,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_duty(DUTY_KB_ADMIN)),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _triggers(server).create_registration(
        _actor(principal),
        _require_uuid(id, "workflow id"),
        kind=body.kind,
        name=body.name,
        cron_expression=body.cron_expression,
        event_name=body.event_name,
        event_filter=body.event_filter,
        tool_grants=body.tool_grants,
        kb_grants=[
            (_require_uuid(grant.kb_id, "knowledge base id"), grant.permission)
            for grant in body.kb_grants
        ],
        tolerance_seconds=body.tolerance_seconds,
        signature_header=body.signature_header,
        timestamp_header=body.timestamp_header,
    )
    return workbuddy_envelope(request, payload)


@router.get(
    "/workflows/{id}/trigger-registrations",
    summary="List trigger registrations without any secret material",
)
async def list_trigger_registrations(
    request: Request,
    id: str,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_duty(DUTY_KB_ADMIN)),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    items = _triggers(server).list_registrations(
        _actor(principal), _require_uuid(id, "workflow id")
    )
    return workbuddy_envelope(request, {"items": items, "count": len(items)})


@router.delete(
    "/workflows/{id}/trigger-registrations/{registration_id}",
    summary="Revoke a trigger registration and its grants",
)
async def delete_trigger_registration(
    request: Request,
    id: str,
    registration_id: str,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_duty(DUTY_KB_ADMIN)),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _triggers(server).revoke_registration(
        _actor(principal),
        _require_uuid(id, "workflow id"),
        _require_uuid(registration_id, "registration id"),
    )
    return workbuddy_envelope(request, payload)


@router.post(
    "/workflows/{id}/trigger-registrations/{registration_id}/rotate-secret",
    summary="Rotate the webhook signing secret (returned exactly once)",
)
async def rotate_trigger_secret(
    request: Request,
    id: str,
    registration_id: str,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_duty(DUTY_KB_ADMIN)),
    server: Any = Depends(get_server),
) -> JSONResponse:
    payload = _triggers(server).rotate_secret(
        _actor(principal),
        _require_uuid(id, "workflow id"),
        _require_uuid(registration_id, "registration id"),
    )
    return JSONResponse(
        content=workbuddy_envelope(request, payload),
        headers={"Cache-Control": "no-store"},
    )


# ── public webhook intake and admin replay ───────────────────────────────────


@router.post(
    "/webhooks/{webhook_path}",
    status_code=202,
    summary="Accept a signed webhook delivery (HMAC over the raw body)",
)
async def ingest_webhook(
    request: Request,
    webhook_path: str,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    raw_body = await request.body()
    payload = _triggers(server).ingest_webhook(
        webhook_path, raw_body=raw_body, headers=dict(request.headers)
    )
    return workbuddy_envelope(request, payload)


class ChatMessageBody(BaseModel):
    """A message in a conversation: the id makes a retried send safe."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)


@router.get(
    "/conversations/{conversation_id}/deliveries",
    summary="What this conversation has triggered",
)
async def list_conversation_deliveries(
    request: Request,
    conversation_id: str,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """The deliveries a conversation produced, so a message's effect is visible.

    Read-only: it answers "did my message run anything, and what became of it",
    which is the question a chat flow has to be able to answer to be trustworthy.
    """
    payload = _triggers(server).list_chat_deliveries(
        _actor(principal), conversation_id=conversation_id, limit=limit
    )
    return workbuddy_envelope(request, payload)


@router.post(
    "/conversations/{conversation_id}/messages",
    status_code=202,
    summary="Post a message that may fire the flows bound to this conversation",
)
async def post_chat_message(
    request: Request,
    conversation_id: str,
    body: ChatMessageBody,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """A chat message raises an event; the flows bound to this conversation answer.

    The endpoint decides nothing about *who* should respond: it raises the event
    and each registration's own filter picks, exactly as the webhook intake does.
    A message that matches nothing is still accepted — a conversation that no
    workflow listens to is a normal conversation, not an error.
    """
    payload = _triggers(server).ingest_chat_message(
        _actor(principal),
        conversation_id=conversation_id,
        message_id=body.message_id,
        text=body.text,
    )
    return workbuddy_envelope(request, payload)


@router.post(
    "/triggers/{registration_id}/test-delivery",
    status_code=202,
    summary="Admin replay: dispatch a test event and record the audit row",
)
async def test_trigger_delivery(
    request: Request,
    registration_id: str,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_duty(DUTY_KB_ADMIN)),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    payload = _triggers(server).test_delivery(
        _actor(principal), _require_uuid(registration_id, "registration id")
    )
    return workbuddy_envelope(request, payload)

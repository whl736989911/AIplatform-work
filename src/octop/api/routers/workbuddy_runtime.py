"""WorkBuddy runtime API: executions, approvals, jobs, notifications, chat.

Every route derives tenant identity from the authenticated WorkBuddy principal
(:mod:`octop.api.routers.workbuddy_identity`) — never from a header or body.
Cross-tenant or otherwise invisible objects answer the uniform not-found, and no
response ever carries a token, token hash, or any secret: approval challenges are
returned exactly once by :func:`challenge_approval_request` and only their hash
is persisted.

Paths are relative to the ``/api/v1`` prefix mounted by the application factory.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPrincipal,
    require_workbuddy_admin,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.pool import DatabasePool
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.runtime import (
    APPROVAL_DECISIONS,
    RECONCILIATION_STATUSES,
    RuntimeActor,
    WorkBuddyRuntimeService,
)

router = APIRouter()

_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]
_AdminPrincipal = Annotated[WorkBuddyPrincipal, Depends(require_workbuddy_admin())]

_DATABASE_NOT_CONFIGURED = "control-plane database not configured yet"
_POSTGRES_REQUIRED = "WorkBuddy runtime requires the PostgreSQL control plane"

_MAX_TEXT = 4_000


def _service(server: Any) -> WorkBuddyRuntimeService:
    """Runtime service bound to the PostgreSQL control plane, or a controlled 503."""
    services = getattr(server, "services", None)
    if services is None:
        raise OctopError(ErrorCode.SETUP_REQUIRED, _DATABASE_NOT_CONFIGURED, status=503)
    db = getattr(services, "db", None)
    if not isinstance(db, DatabasePool) or db.dialect != "postgresql":
        raise OctopError(ErrorCode.WORKBUDDY_POSTGRES_REQUIRED, _POSTGRES_REQUIRED)
    return WorkBuddyRuntimeService(db)


def _actor(principal: WorkBuddyPrincipal) -> RuntimeActor:
    return RuntimeActor(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        role="admin" if principal.is_admin else "member",
        tenant_status=principal.tenant_status,
        department_id=principal.department_id,
    )


def _uuid(value: str, *, field: str) -> str:
    """Reject malformed identifiers before any query (uniform not-found)."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise OctopError(ErrorCode.NOT_FOUND, f"{field} not found") from None


class ExecuteBody(BaseModel):
    """Workflow inputs plus an optional idempotency key for external writes."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_request_id: str = Field(min_length=36, max_length=36)
    decision: str = Field(min_length=1, max_length=16)
    token: str = Field(min_length=1, max_length=200)


class ReconciliationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=16)
    evidence: dict[str, Any] = Field(default_factory=dict)
    external_ref: str | None = Field(default=None, max_length=200)


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=_MAX_TEXT)
    session_id: str | None = Field(default=None, min_length=36, max_length=36)


# --------------------------------------------------------------------------- #
# executions
# --------------------------------------------------------------------------- #


@router.post("/workflows/{workflow_id}/execute", status_code=202, summary="Accept an execution")
async def execute_workflow(
    workflow_id: str,
    body: ExecuteBody,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """Accept a run of the workflow's active immutable version.

    ``Idempotency-Key`` (header or body) makes an external write replayable: the
    same key with the same request returns the same execution, a different
    request under the same key conflicts.
    """
    view = _service(server).start_execution(
        _actor(principal),
        workflow_id=_uuid(workflow_id, field="workflow"),
        inputs=body.inputs,
        trigger_type="api",
        idempotency_scope="workflow-execute",
        idempotency_key=body.idempotency_key or idempotency_header,
    )
    return workbuddy_envelope(request, view.to_payload())


@router.get("/executions", summary="List executions")
async def list_executions(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    scope: str = Query(default="self", pattern="^(self|tenant)$"),
    workflow_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_executions(
        _actor(principal),
        scope=scope,
        workflow_id=_uuid(workflow_id, field="workflow") if workflow_id else None,
        status=status,
        limit=limit,
    )
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.get("/executions/{execution_id}", summary="Execution detail")
async def get_execution(
    execution_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    view, steps, edges = _service(server).execution_facts(
        _actor(principal), _uuid(execution_id, field="execution")
    )
    payload = view.to_payload()
    payload["steps"] = [step.to_payload() for step in steps]
    payload["edges"] = edges
    return workbuddy_envelope(request, payload)


@router.post(
    "/executions/{execution_id}/cancel", status_code=202, summary="Cancel an execution"
)
async def cancel_execution(
    execution_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    view = _service(server).cancel_execution(
        _actor(principal), _uuid(execution_id, field="execution")
    )
    return workbuddy_envelope(request, view.to_payload())


@router.post("/executions/{execution_id}/resume", summary="Submit an approval decision")
async def resume_execution(
    execution_id: str,
    body: ResumeBody,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    if body.decision not in APPROVAL_DECISIONS:
        raise OctopError(ErrorCode.WORKBUDDY_VALIDATION_FAILED, "decision is not supported")
    view = _service(server).resume_execution(
        _actor(principal),
        _uuid(execution_id, field="execution"),
        approval_request_id=_uuid(body.approval_request_id, field="approval request"),
        decision=body.decision,
        token=body.token,
    )
    return workbuddy_envelope(request, view.to_payload())


@router.post(
    "/executions/{execution_id}/reconciliations", summary="Record external-write evidence"
)
async def record_reconciliation(
    execution_id: str,
    body: ReconciliationBody,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    if body.status not in RECONCILIATION_STATUSES:
        raise OctopError(
            ErrorCode.WORKBUDDY_VALIDATION_FAILED, "reconciliation status is not supported"
        )
    payload = _service(server).record_reconciliation(
        _actor(principal),
        _uuid(execution_id, field="execution"),
        node_id=body.node_id,
        status=body.status,
        evidence=body.evidence,
        external_ref=body.external_ref,
    )
    return workbuddy_envelope(request, dict(payload))


@router.get("/executions/{execution_id}/reconciliations", summary="List reconciliation evidence")
async def list_reconciliations(
    execution_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    items = _service(server).list_reconciliations(
        _actor(principal), _uuid(execution_id, field="execution")
    )
    return workbuddy_envelope(request, {"items": items})


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #


@router.get("/approval-requests", summary="List approval requests")
async def list_approval_requests(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    scope: str = Query(default="self", pattern="^(self|tenant)$"),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_approval_requests(
        _actor(principal), status=status, scope=scope, limit=limit
    )
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.get("/approval-requests/{approval_request_id}", summary="Approval request detail")
async def get_approval_request(
    approval_request_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    service = _service(server)
    actor = _actor(principal)
    view = service.get_approval_request(
        actor, _uuid(approval_request_id, field="approval request")
    )
    payload = view.to_payload()
    payload["candidates"] = [
        {"user_id": candidate.user_id, "status": candidate.status}
        for candidate in service.approval_candidates(actor, view.id)
    ]
    return workbuddy_envelope(request, payload)


@router.post(
    "/approval-requests/{approval_request_id}/challenge",
    summary="Issue a two-minute one-time approval challenge",
)
async def challenge_approval_request(
    approval_request_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Return the one-time token once; only its hash is stored (no-store)."""
    token = _service(server).issue_approval_challenge(
        _actor(principal), _uuid(approval_request_id, field="approval request")
    )
    response = workbuddy_envelope(request, {"approval_request_id": approval_request_id})
    response["token"] = token
    response["expires_in"] = 120
    return response


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


@router.get("/jobs", summary="List asynchronous jobs")
async def list_jobs(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_jobs(_actor(principal), status=status, limit=limit)
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.get("/jobs/{job_id}", summary="Job status")
async def get_job(
    job_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    view = _service(server).get_job(_actor(principal), _uuid(job_id, field="job"))
    return workbuddy_envelope(request, view.to_payload())


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #


@router.get("/notifications", summary="List own notifications")
async def list_notifications(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_notifications(
        _actor(principal), unread_only=unread_only, limit=limit
    )
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.post("/notifications/{notification_id}/read", summary="Mark own notification read")
async def read_notification(
    notification_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    view = _service(server).mark_notification_read(
        _actor(principal), _uuid(notification_id, field="notification")
    )
    return workbuddy_envelope(request, view.to_payload())


# --------------------------------------------------------------------------- #
# private chat
# --------------------------------------------------------------------------- #


@router.post("/chat", summary="Single private chat turn")
async def chat(
    body: ChatBody,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Answer one turn in the caller's private session.

    Without a configured, approved model revision this fails closed with
    ``MODEL_NOT_CONFIGURED`` — the user turn is recorded, nothing is simulated.
    """
    view = _service(server).chat(
        _actor(principal),
        message=body.message,
        session_id=_uuid(body.session_id, field="chat session") if body.session_id else None,
    )
    return workbuddy_envelope(request, view.to_payload())


@router.get("/chat-sessions", summary="List own private chat sessions")
async def list_chat_sessions(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_chat_sessions(_actor(principal), limit=limit)
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.get("/chat-sessions/{session_id}", summary="Read own private chat session")
async def get_chat_session(
    session_id: str,
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    view = _service(server).get_chat_session(
        _actor(principal), _uuid(session_id, field="chat session")
    )
    return workbuddy_envelope(request, view.to_payload())


# --------------------------------------------------------------------------- #
# audit and usage
# --------------------------------------------------------------------------- #


@router.get("/audit-logs", summary="Query tenant audit records")
async def list_audit_logs(
    request: Request,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
    action: str | None = Query(default=None),
    resource_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    views = _service(server).list_audit_logs(
        _actor(principal), action=action, resource_type=resource_type, limit=limit
    )
    return workbuddy_envelope(request, {"items": [view.to_payload() for view in views]})


@router.get("/usage", summary="Execution, job and quota usage")
async def usage(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    days: int = Query(default=30, ge=1, le=365),
) -> dict[str, Any]:
    payload = _service(server).usage(_actor(principal), days=days)
    return workbuddy_envelope(request, dict(payload))


__all__ = ["router"]

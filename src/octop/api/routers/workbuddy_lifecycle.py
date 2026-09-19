"""WorkBuddy tenant lifecycle API: controlled export, one-time redeem, deletion.

Paths are relative to the ``/api/v1`` mount (see ``contracts/route-manifest.json``):

* ``POST /tenants/{id}/export`` — 202, builds the redacted export and mints the
  single redeem token for the fixed 72h window;
* ``POST /exports/{id}/redeem`` — 200, one-time CAS consumption of that token;
* ``GET  /exports/{id}/manifest`` — the frozen manifest and its digest;
* ``POST /tenants/{id}/deletion-requests`` — 201 inside cooling-off, or
  ``COMPLIANCE_GATE_CLOSED`` when no signed policy authorises the tenant;
* ``GET  /tenants/{id}/deletion-requests/{request_id}`` — stage and timestamps;
* ``POST /tenants/{id}/deletion-requests/{request_id}/cancel`` — CAS cancel,
  only while the cooling-off window is open.

Tenant identity always comes from the authenticated principal: the ``{id}`` in
the path must equal the principal's tenant and anything else is reported as a
uniform 404.  Deletion requests additionally require a recent login (``iat``
within :data:`RECENT_AUTH_MAX_AGE_SECONDS`, otherwise 428) and never queue work
when the compliance gate is closed.  Export payloads and redeem tokens are
returned ``no-store`` and are never logged.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import InvalidToken, TokenExpired, decode_claims, extract_raw_token, get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPrincipal,
    require_workbuddy_admin,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.workbuddy_context import WorkBuddyPostgresRequiredError
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy import lifecycle as policy

router = APIRouter()

_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]
_AdminPrincipal = Annotated[WorkBuddyPrincipal, Depends(require_workbuddy_admin())]

RECENT_AUTH_MAX_AGE_SECONDS = 3600

_EXPORT_NOT_FOUND = "export job not found"
_DELETION_NOT_FOUND = "deletion request not found"
_NO_STORE = "no-store"


class RedeemBody(BaseModel):
    token: str = Field(min_length=8, max_length=512)


class DeletionCancelBody(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)


def _repo(server: Any) -> Any:
    from octop.infra.db.repos.workbuddy_lifecycle import WorkBuddyLifecycleRepo

    return WorkBuddyLifecycleRepo(server.services.db)


def _not_found(detail: str) -> OctopError:
    return OctopError(ErrorCode.NOT_FOUND, detail)


def _public_id(value: str, detail: str) -> str:
    """Normalize a public id; a malformed id looks exactly like an absent one."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _not_found(detail) from exc


def _own_tenant(path_tenant_id: str, principal: WorkBuddyPrincipal) -> str:
    """A tenant admin only ever reaches their own tenant, and others get a 404."""
    tenant_id = _public_id(path_tenant_id, "tenant not found")
    if tenant_id != str(principal.tenant_id):
        raise _not_found("tenant not found")
    return tenant_id


def _require_recent_auth(request: Request, server: Any) -> None:
    """Deletion and cancel require a fresh login (428 when the token is old)."""
    raw = extract_raw_token(authorization=request.headers.get("authorization"))
    if not raw:
        raise OctopError(
            ErrorCode.PRECONDITION_REQUIRED, "recent authentication required for this action"
        )
    try:
        claims = decode_claims(server, raw)
    except (InvalidToken, TokenExpired) as exc:
        raise OctopError(
            ErrorCode.PRECONDITION_REQUIRED, "recent authentication required for this action"
        ) from exc
    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)):
        raise OctopError(
            ErrorCode.PRECONDITION_REQUIRED, "recent authentication required for this action"
        )
    if int(time.time()) - int(issued_at) > RECENT_AUTH_MAX_AGE_SECONDS:
        raise OctopError(
            ErrorCode.PRECONDITION_REQUIRED,
            "recent authentication required for this action; sign in again",
        )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["Pragma"] = _NO_STORE


def _translate(exc: WorkBuddyPostgresRequiredError) -> OctopError:
    return OctopError(ErrorCode.WORKBUDDY_POSTGRES_REQUIRED, str(exc))


# ── export ──────────────────────────────────────────────────────────────────


@router.post("/tenants/{tenant_id}/export", status_code=202, summary="Request a tenant data export")
async def request_tenant_export(
    request: Request,
    tenant_id: str,
    response: Response,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Build the redacted export and return the job id plus its one redeem token."""
    tenant = _own_tenant(tenant_id, principal)
    try:
        issue = policy.start_tenant_export(
            _repo(server), tenant_id=tenant, user_id=principal.user_id
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    _no_store(response)
    return workbuddy_envelope(request, issue.as_payload())


@router.get("/exports/{export_job_id}/manifest", summary="Read the frozen export manifest")
async def read_export_manifest(
    request: Request,
    export_job_id: str,
    response: Response,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    job_id = _public_id(export_job_id, _EXPORT_NOT_FOUND)
    try:
        manifest = policy.read_export_manifest(
            _repo(server), tenant_id=str(principal.tenant_id), export_job_id=job_id
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    if manifest is None:
        raise _not_found(_EXPORT_NOT_FOUND)
    _no_store(response)
    return workbuddy_envelope(request, manifest)


@router.post("/exports/{export_job_id}/redeem", summary="Redeem an export token once")
async def redeem_export(
    request: Request,
    export_job_id: str,
    response: Response,
    body: RedeemBody,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Consume the one-time token; a second attempt is refused, never replayed."""
    job_id = _public_id(export_job_id, _EXPORT_NOT_FOUND)
    try:
        download = policy.redeem_export(
            _repo(server),
            tenant_id=str(principal.tenant_id),
            user_id=principal.user_id,
            token=body.token,
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    if download.export_job_id != job_id:
        # The token belongs to another job of this tenant: report it as not found.
        raise _not_found(_EXPORT_NOT_FOUND)
    _no_store(response)
    return workbuddy_envelope(
        request,
        {
            "export_job_id": download.export_job_id,
            "manifest": download.manifest,
            "manifest_sha256": download.manifest_sha256,
            "tables": list(download.tables),
            "redeemed_at": download.redeemed_at,
        },
    )


# ── deletion ────────────────────────────────────────────────────────────────


@router.post(
    "/tenants/{tenant_id}/deletion-requests",
    status_code=201,
    summary="Request tenant deletion (30-day cooling-off)",
)
async def create_deletion_request(
    request: Request,
    tenant_id: str,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Open the cooling-off window; without a signed policy this answers 503."""
    tenant = _own_tenant(tenant_id, principal)
    _require_recent_auth(request, server)
    try:
        view = policy.request_tenant_deletion(
            _repo(server), tenant_id=tenant, user_id=principal.user_id
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    return workbuddy_envelope(request, view.as_payload())


@router.get(
    "/tenants/{tenant_id}/deletion-requests/{deletion_request_id}",
    summary="Read a deletion request stage and timestamps",
)
async def read_deletion_request(
    request: Request,
    tenant_id: str,
    deletion_request_id: str,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    tenant = _own_tenant(tenant_id, principal)
    request_id = _public_id(deletion_request_id, _DELETION_NOT_FOUND)
    try:
        view = policy.read_deletion_request(
            _repo(server), tenant_id=tenant, deletion_request_id=request_id
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    return workbuddy_envelope(request, view.as_payload())


@router.post(
    "/tenants/{tenant_id}/deletion-requests/{deletion_request_id}/cancel",
    summary="Cancel a deletion request inside its cooling-off window",
)
async def cancel_deletion_request(
    request: Request,
    tenant_id: str,
    deletion_request_id: str,
    principal: _AdminPrincipal,
    server: Any = Depends(get_server),
    body: Annotated[DeletionCancelBody | None, Body()] = None,
) -> dict[str, Any]:
    """CAS cancel: only a live cooling-off request with a matching version wins."""
    tenant = _own_tenant(tenant_id, principal)
    request_id = _public_id(deletion_request_id, _DELETION_NOT_FOUND)
    _require_recent_auth(request, server)
    expected_version = None if body is None else body.expected_version
    try:
        view = policy.cancel_tenant_deletion(
            _repo(server),
            tenant_id=tenant,
            user_id=principal.user_id,
            deletion_request_id=request_id,
            expected_version=expected_version,
        )
    except WorkBuddyPostgresRequiredError as exc:
        raise _translate(exc) from exc
    return workbuddy_envelope(request, view.as_payload())

"""WorkBuddy connector credentials, platform catalog, and tenant capabilities.

Tenant identity always comes from the authenticated WorkBuddy principal derived
by the identity slice — never from a header or body field.  Credential material
never crosses this module: the raw secret is encrypted with the Octop connector
key material and written to :class:`~octop.infra.db.repos.secrets.SecretRepo`
under a server-generated reference; only that reference reaches
``WorkBuddyCatalogRepo``, and it is never serialized into a response.

If the configured secret backend cannot guarantee such a reference (an external
Vault-style backend is declared but no adapter is wired, or the local secret
store cannot encrypt) the credential routes fail closed with 503 instead of
persisting the secret locally.
"""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPlatformPrincipal,
    WorkBuddyPrincipal,
    require_platform_audience,
    require_workbuddy_admin,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.connectors.crypto import encrypt_credentials
from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_catalog import (
    WorkBuddyCapabilityNotApproved,
    WorkBuddyCasConflict,
    WorkBuddyCatalogError,
    WorkBuddyCredentialNameTaken,
    WorkBuddyMembershipRequired,
    WorkBuddyPlatformRevisionConflict,
    WorkBuddyRevisionRevoked,
)
from octop.infra.errors import ErrorCode, OctopError

router = APIRouter()

_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]
_AdminPrincipal = Annotated[WorkBuddyPrincipal, Depends(require_workbuddy_admin())]
_PlatformPrincipal = Annotated[WorkBuddyPlatformPrincipal, Depends(require_platform_audience())]

# Every catalog-store refusal carries a stable code; any of them may surface
# from any write, so one tuple covers all call sites.
_CATALOG_ERRORS = (
    WorkBuddyCatalogError,
    WorkBuddyCapabilityNotApproved,
    WorkBuddyCasConflict,
    WorkBuddyCredentialNameTaken,
    WorkBuddyMembershipRequired,
    WorkBuddyPlatformRevisionConflict,
    WorkBuddyRevisionRevoked,
)

# Secret backends that can hand back a durable server-side reference.  ``vault``
# is declared by operators but has no adapter in this deployment, so a
# deployment that asks for it must fail closed instead of persisting locally.
_SUPPORTED_SECRET_BACKENDS = frozenset({"octop"})
_SECRET_BACKEND_ENV = "WORKBUDDY_SECRET_BACKEND"
_VAULT_ADDR_ENV = "VAULT_ADDR"

_SECRET_REF_SCHEME = "octop-secret://"

_SECRET_STORE_UNAVAILABLE = "workbuddy secret store is unavailable"
_SECRET_BACKEND_UNSUPPORTED = (
    "configured secret backend cannot guarantee an external credential reference"
)

_CREDENTIAL_NOT_FOUND = "connector credential not found"
_GRANT_NOT_FOUND = "connector credential grant not found"
_GRANT_TARGET_NOT_FOUND = "connector credential or same-tenant grant target not found"
_TOOL_NOT_FOUND = "platform tool revision not found"
_MODEL_NOT_FOUND = "platform model revision not found"


def _not_found(detail: str) -> OctopError:
    return OctopError(ErrorCode.RESOURCE_NOT_FOUND, detail)


def _public_id(value: str, detail: str) -> str:
    """Normalize a public WorkBuddy id; malformed ids look exactly like absent ones."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _not_found(detail) from exc


def _repo(server: Any) -> Any:
    """Instantiate the catalog repo, failing closed without the PostgreSQL control plane.

    Mirrors the identity slice: a SQLite control plane answers 503 rather than
    pretending a tenant store exists.
    """
    services = getattr(server, "services", None)
    if services is None:
        raise OctopError(
            ErrorCode.SETUP_REQUIRED,
            "control-plane database not configured yet",
            status=503,
        )
    db = getattr(services, "db", None)
    if getattr(db, "dialect", "") != "postgresql":
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "WorkBuddy connector governance requires the PostgreSQL control plane",
        )
    from octop.infra.db.repos.workbuddy_catalog import WorkBuddyCatalogRepo

    # ``dialect`` was checked above, so this is a real PostgreSQL pool.
    assert isinstance(db, DatabasePool), db
    return WorkBuddyCatalogRepo(db)


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #


def _require_secret_backend(server: Any) -> Any:
    """Return the secret store that can serve a server-generated reference."""
    declared = (os.environ.get(_SECRET_BACKEND_ENV) or "").strip().lower()
    if not declared:
        # No explicit backend chosen: a declared Vault address means the operator
        # expects an external secret manager this deployment cannot serve.
        declared = "vault" if (os.environ.get(_VAULT_ADDR_ENV) or "").strip() else "octop"
    if declared not in _SUPPORTED_SECRET_BACKENDS:
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, _SECRET_BACKEND_UNSUPPORTED
        )
    secret_repo = getattr(getattr(server, "services", None), "secret_repo", None)
    if secret_repo is None:
        raise OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, _SECRET_STORE_UNAVAILABLE)
    return secret_repo


def _new_secret_reference(scope: str, revision: int) -> tuple[str, str]:
    """Server-generated secret-store key plus the metadata pointer that names it.

    The pointer is URI-shaped because the catalog store accepts credential
    references only as opaque ``scheme://path`` pointers, and it carries no secret
    material — just the key whose encrypted value lives in the secret store.  The
    random suffix keeps concurrent rotations of one revision apart: a losing
    compare-and-swap can never leave the winning revision pointing at another
    attempt's material.
    """
    key = f"workbuddy.credential.{scope}.r{revision}.{uuid.uuid4().hex}"
    return key, f"{_SECRET_REF_SCHEME}{key}"


def _store_credential_secret(secret_repo: Any, key: str, secret: dict[str, Any] | str) -> None:
    """Encrypt and persist credential material; only the pointer ever leaves here."""
    payload = {"value": secret} if isinstance(secret, str) else dict(secret)
    try:
        blob = encrypt_credentials(secret_repo, payload)
        secret_repo.get_or_create(key, lambda: blob)
    except Exception as exc:  # noqa: BLE001 - any key/store failure must fail closed
        raise OctopError(
            ErrorCode.DEPENDENCY_UNAVAILABLE, _SECRET_STORE_UNAVAILABLE
        ) from exc


# Class-level fallbacks for refusals raised without an operation-specific code.
_CATALOG_FALLBACK_CODES: dict[type[Exception], ErrorCode] = {
    WorkBuddyCapabilityNotApproved: ErrorCode.FORBIDDEN_ROLE,
    WorkBuddyCredentialNameTaken: ErrorCode.WORKBUDDY_CREDENTIAL_NAME_TAKEN,
    WorkBuddyMembershipRequired: ErrorCode.FORBIDDEN_ROLE,
    WorkBuddyPlatformRevisionConflict: ErrorCode.WORKBUDDY_PLATFORM_REVISION_CONFLICT,
}


def _catalog_refusal(exc: Exception, *, conflict: ErrorCode, revoked: ErrorCode) -> OctopError:
    """Re-raise a catalog-store refusal with the operation's stable code.

    The store attaches the precise code (WORKBUDDY_CREDENTIAL_*, WORKBUDDY_CAPABILITY_*,
    WORKBUDDY_PLATFORM_*) to each refusal; when it only says *what* went wrong, the
    operation supplies the corresponding credential/platform/capability code.  A refusal
    this build cannot classify stays an internal error rather than a misleading status.
    """
    raw = str(getattr(exc, "code", "") or "")
    try:
        code = ErrorCode(raw)
    except ValueError:
        if isinstance(exc, WorkBuddyCasConflict):
            code = conflict
        elif isinstance(exc, WorkBuddyRevisionRevoked):
            code = revoked
        else:
            code = _CATALOG_FALLBACK_CODES.get(type(exc), ErrorCode.INTERNAL_ERROR)
    return OctopError(code, str(getattr(exc, "message", "") or exc))


def _credential_refusal(exc: Exception) -> OctopError:
    return _catalog_refusal(
        exc,
        conflict=ErrorCode.WORKBUDDY_CREDENTIAL_REVISION_CONFLICT,
        revoked=ErrorCode.WORKBUDDY_CREDENTIAL_REVOKED,
    )


def _platform_refusal(exc: Exception) -> OctopError:
    return _catalog_refusal(
        exc,
        conflict=ErrorCode.WORKBUDDY_PLATFORM_REVISION_CONFLICT,
        revoked=ErrorCode.WORKBUDDY_PLATFORM_REVISION_REVOKED,
    )


def _capability_refusal(exc: Exception) -> OctopError:
    return _catalog_refusal(
        exc,
        conflict=ErrorCode.WORKBUDDY_CAPABILITY_REVISION_CONFLICT,
        revoked=ErrorCode.FORBIDDEN_ROLE,
    )


# --------------------------------------------------------------------------- #
# payload projections — metadata only, never secret material or references
# --------------------------------------------------------------------------- #


def _credential_payload(record: Any) -> dict[str, Any]:
    revision = int(record.revision)
    return {
        "id": record.credential_id,
        "connector_type": record.connector_kind,
        "display_name": record.name,
        "description": record.description,
        "owner_id": record.owner_member_id,
        "status": record.status,
        "revision": revision,
        "allowed_scopes": list(record.scopes),
        # Last mutation of a multi-revision credential; None before the first rotation.
        "rotated_at": int(record.updated_at) if revision > 1 else None,
        "revoked_at": record.revoked_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _grant_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.grant_id,
        "credential_id": record.credential_id,
        "user_id": record.member_id,
        "granted_by": record.granted_by_member_id,
        "created_at": record.created_at,
    }


def _tool_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.tool_revision_id,
        "tool_key": record.tool_key,
        "adapter_key": record.adapter_key,
        "display_name": record.display_name,
        "description": record.description,
        "revision": record.revision,
        "status": record.status,
        "published_at": record.published_at,
        "revoked_at": record.revoked_at,
    }


def _model_payload(record: Any) -> dict[str, Any]:
    return {
        "id": record.model_revision_id,
        "model_key": record.model_key,
        "adapter_key": record.adapter_key,
        "display_name": record.display_name,
        "description": record.description,
        "revision": record.revision,
        "status": record.status,
        "published_at": record.published_at,
        "revoked_at": record.revoked_at,
    }


def _capabilities_payload(tenant_id: str, record: Any | None) -> dict[str, Any]:
    if record is None:
        return {
            "tenant_id": tenant_id,
            "tool_ids": [],
            "model_ids": [],
            "default_model_id": None,
            "revision": 0,
            "updated_at": None,
        }
    return {
        "tenant_id": record.tenant_id,
        "tool_ids": list(record.tool_revision_ids),
        "model_ids": list(record.model_revision_ids),
        "default_model_id": record.default_model_revision_id,
        "revision": record.revision,
        "updated_at": record.updated_at,
    }


# --------------------------------------------------------------------------- #
# request bodies
# --------------------------------------------------------------------------- #


class CredentialCreateBody(BaseModel):
    """Create a credential owned by the calling member.

    ``external_ref`` is intentionally absent: the reference is server-generated,
    and unknown extra keys (including a client-supplied reference) are rejected.
    """

    model_config = ConfigDict(extra="forbid")

    connector_type: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    secret: dict[str, Any] | str = Field(min_length=1)
    allowed_scopes: list[str] = Field(default_factory=list)
    description: str | None = Field(default=None, max_length=500)


class CredentialRotateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: dict[str, Any] | str = Field(min_length=1)
    expected_revision: int | None = Field(default=None, ge=1)


class GrantCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=64)


class ToolPublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_key: str = Field(min_length=1, max_length=120)
    adapter_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class ModelPublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_key: str = Field(min_length=1, max_length=120)
    adapter_key: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class CapabilitiesBody(BaseModel):
    """Atomic replacement of the tenant's approved revisions and default model."""

    model_config = ConfigDict(extra="forbid")

    tool_ids: list[str] = Field(default_factory=list)
    model_ids: list[str] = Field(default_factory=list)
    default_model_id: str | None = Field(default=None, max_length=64)
    expected_revision: int | None = Field(default=None, ge=1)


# --------------------------------------------------------------------------- #
# connector credentials
# --------------------------------------------------------------------------- #


def _load_credential(
    repo: Any,
    principal: WorkBuddyPrincipal,
    credential_id: str,
    *,
    allow_admin: bool,
) -> Any:
    """Load a credential the caller owns (or administers).

    Unknown, cross-tenant, and not-owned-or-administered credentials are all
    indistinguishable from absent: 404, never 403.
    """
    credential = repo.get_credential(
        principal.tenant_id, _public_id(credential_id, _CREDENTIAL_NOT_FOUND)
    )
    if credential is None:
        raise _not_found(_CREDENTIAL_NOT_FOUND)
    if credential.owner_member_id != principal.member_id and not (
        allow_admin and principal.is_admin
    ):
        raise _not_found(_CREDENTIAL_NOT_FOUND)
    return credential


@router.get("/connector-credentials", summary="List connector credential metadata")
async def list_connector_credentials(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Own credentials plus, for tenant admins, every credential in the tenant."""
    repo = _repo(server)
    rows = repo.list_credentials(principal.tenant_id)
    if not principal.is_admin:
        rows = [row for row in rows if row.owner_member_id == principal.member_id]
    return workbuddy_envelope(request, {"items": [_credential_payload(row) for row in rows]})


@router.post("/connector-credentials", status_code=201, summary="Create a connector credential")
async def create_connector_credential(
    request: Request,
    body: CredentialCreateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Create credential metadata backed by a server-generated secret reference."""
    secret_repo = _require_secret_backend(server)
    repo = _repo(server)
    # The secret is written first: a metadata row without material would advertise a
    # credential nothing can dispatch, while an unreferenced secret is inert.
    key, reference = _new_secret_reference(uuid.uuid4().hex, 1)
    _store_credential_secret(secret_repo, key, body.secret)
    try:
        record = repo.create_credential(
            principal.tenant_id,
            name=body.display_name,
            connector_kind=body.connector_type,
            external_ref=reference,
            actor_member_id=principal.member_id,
            description=body.description or "",
            scopes=tuple(body.allowed_scopes),
        )
    except _CATALOG_ERRORS as exc:
        raise _credential_refusal(exc) from exc
    return workbuddy_envelope(request, _credential_payload(record))


@router.post(
    "/connector-credentials/{credential_id}/rotate", summary="Publish a new credential revision"
)
async def rotate_connector_credential(
    request: Request,
    credential_id: str,
    body: CredentialRotateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Rotate credential material as the owning member, compare-and-swap on revision."""
    secret_repo = _require_secret_backend(server)
    repo = _repo(server)
    credential = _load_credential(repo, principal, credential_id, allow_admin=False)
    if credential.status == "revoked":
        raise OctopError(
            ErrorCode.WORKBUDDY_CREDENTIAL_REVOKED, "credential is revoked; rotation refused"
        )
    key, reference = _new_secret_reference(credential.credential_id, int(credential.revision) + 1)
    _store_credential_secret(secret_repo, key, body.secret)
    try:
        record = repo.rotate_credential(
            principal.tenant_id,
            credential.credential_id,
            external_ref=reference,
            actor_member_id=principal.member_id,
            expected_revision=body.expected_revision or int(credential.revision),
        )
    except _CATALOG_ERRORS as exc:
        raise _credential_refusal(exc) from exc
    if record is None:
        raise _not_found(_CREDENTIAL_NOT_FOUND)
    return workbuddy_envelope(request, _credential_payload(record))


@router.post("/connector-credentials/{credential_id}/revoke", summary="Revoke a credential")
async def revoke_connector_credential(
    request: Request,
    credential_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Revoke the credential as its owner or a tenant admin; secrets stay hidden."""
    repo = _repo(server)
    credential = _load_credential(repo, principal, credential_id, allow_admin=True)
    record = repo.revoke_credential(
        principal.tenant_id, credential.credential_id, actor_member_id=principal.member_id
    )
    if record is None:
        raise _not_found(_CREDENTIAL_NOT_FOUND)
    return workbuddy_envelope(request, _credential_payload(record))


@router.get("/connector-credentials/{credential_id}/grants", summary="List credential grants")
async def list_credential_grants(
    request: Request,
    credential_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Owners list who may use the credential; only the use capability is exposed."""
    repo = _repo(server)
    credential = _load_credential(repo, principal, credential_id, allow_admin=False)
    rows = repo.list_grants(principal.tenant_id, credential.credential_id)
    return workbuddy_envelope(request, {"items": [_grant_payload(row) for row in rows]})


@router.post(
    "/connector-credentials/{credential_id}/grants",
    status_code=201,
    summary="Grant a same-tenant user use of a credential",
)
async def create_credential_grant(
    request: Request,
    credential_id: str,
    body: GrantCreateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Grant use capability without disclosing the credential material."""
    repo = _repo(server)
    credential = _load_credential(repo, principal, credential_id, allow_admin=False)
    if credential.status == "revoked":
        raise OctopError(
            ErrorCode.WORKBUDDY_CREDENTIAL_REVOKED, "credential is revoked; grant refused"
        )
    try:
        grant = repo.create_grant(
            principal.tenant_id,
            credential.credential_id,
            target_member_id=_public_id(body.user_id, _GRANT_NOT_FOUND),
            actor_member_id=principal.member_id,
        )
    except _CATALOG_ERRORS as exc:
        raise _credential_refusal(exc) from exc
    if grant is None:
        raise _not_found(_GRANT_TARGET_NOT_FOUND)
    return workbuddy_envelope(request, _grant_payload(grant))


@router.delete(
    "/connector-credentials/{credential_id}/grants/{user_id}",
    summary="Revoke a credential grant",
)
async def delete_credential_grant(
    request: Request,
    credential_id: str,
    user_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Revoke use capability; later dispatch attempts must fail authorization."""
    repo = _repo(server)
    credential = _load_credential(repo, principal, credential_id, allow_admin=False)
    member_id = _public_id(user_id, _GRANT_NOT_FOUND)
    if not repo.delete_grant(principal.tenant_id, credential.credential_id, member_id):
        raise _not_found(_GRANT_NOT_FOUND)
    return workbuddy_envelope(
        request,
        {"credential_id": credential.credential_id, "user_id": member_id, "revoked": True},
    )


# --------------------------------------------------------------------------- #
# platform catalog (explicit workbuddy-platform audience)
# --------------------------------------------------------------------------- #


@router.post("/platform/tools", status_code=201, summary="Publish a platform tool revision")
async def publish_platform_tool(
    request: Request,
    body: ToolPublishBody,
    platform: _PlatformPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Publish an immutable tool revision; only the adapter key is registered."""
    repo = _repo(server)
    try:
        record = repo.publish_tool(
            tool_key=body.tool_key,
            adapter_key=body.adapter_key,
            display_name=body.display_name,
            description=body.description or "",
            actor_user_id=platform.user_id,
        )
    except _CATALOG_ERRORS as exc:
        raise _platform_refusal(exc) from exc
    return workbuddy_envelope(request, _tool_payload(record))


@router.post("/platform/tools/{tool_id}/revoke", summary="Revoke a platform tool revision")
async def revoke_platform_tool(
    request: Request,
    tool_id: str,
    platform: _PlatformPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    revision_id = _public_id(tool_id, _TOOL_NOT_FOUND)
    try:
        revoked = repo.revoke_tool(revision_id, actor_user_id=platform.user_id)
    except _CATALOG_ERRORS as exc:
        raise _platform_refusal(exc) from exc
    if not revoked:
        raise _not_found(_TOOL_NOT_FOUND)
    record = repo.get_tool_revision(revision_id)
    if record is None:
        raise _not_found(_TOOL_NOT_FOUND)
    return workbuddy_envelope(request, _tool_payload(record))


@router.post("/platform/models", status_code=201, summary="Publish a platform model revision")
async def publish_platform_model(
    request: Request,
    body: ModelPublishBody,
    platform: _PlatformPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    try:
        record = repo.publish_model(
            model_key=body.model_key,
            adapter_key=body.adapter_key,
            display_name=body.display_name,
            description=body.description or "",
            actor_user_id=platform.user_id,
        )
    except _CATALOG_ERRORS as exc:
        raise _platform_refusal(exc) from exc
    return workbuddy_envelope(request, _model_payload(record))


@router.post("/platform/models/{model_id}/revoke", summary="Revoke a platform model revision")
async def revoke_platform_model(
    request: Request,
    model_id: str,
    platform: _PlatformPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    revision_id = _public_id(model_id, _MODEL_NOT_FOUND)
    try:
        revoked = repo.revoke_model(revision_id, actor_user_id=platform.user_id)
    except _CATALOG_ERRORS as exc:
        raise _platform_refusal(exc) from exc
    if not revoked:
        raise _not_found(_MODEL_NOT_FOUND)
    record = repo.get_model_revision(revision_id)
    if record is None:
        raise _not_found(_MODEL_NOT_FOUND)
    return workbuddy_envelope(request, _model_payload(record))


# --------------------------------------------------------------------------- #
# tenant catalog and capabilities
# --------------------------------------------------------------------------- #


@router.get("/tool-catalog", summary="Read the tenant's approved tool catalog")
async def read_tool_catalog(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Published, unrevoked tool revisions the tenant has approved."""
    repo = _repo(server)
    capabilities = repo.get_capabilities(principal.tenant_id)
    approved = set(capabilities.tool_revision_ids) if capabilities is not None else set()
    rows = [row for row in repo.list_public_tools() if row.tool_revision_id in approved]
    return workbuddy_envelope(request, {"items": [_tool_payload(row) for row in rows]})


@router.get("/model-catalog", summary="Read the tenant's approved model catalog")
async def read_model_catalog(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Published, unrevoked model revisions the tenant has approved."""
    repo = _repo(server)
    capabilities = repo.get_capabilities(principal.tenant_id)
    approved = set(capabilities.model_revision_ids) if capabilities is not None else set()
    rows = [row for row in repo.list_public_models() if row.model_revision_id in approved]
    return workbuddy_envelope(request, {"items": [_model_payload(row) for row in rows]})


@router.get("/tenant-capabilities", summary="Read tenant tool and model allowances")
async def read_tenant_capabilities(
    request: Request,
    admin: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    repo = _repo(server)
    record = repo.get_capabilities(admin.tenant_id)
    return workbuddy_envelope(request, _capabilities_payload(admin.tenant_id, record))


@router.put(
    "/tenant-capabilities", summary="Atomically replace tenant allowances and default model"
)
async def update_tenant_capabilities(
    request: Request,
    body: CapabilitiesBody,
    admin: _AdminPrincipal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Replace approvals as a tenant admin; ids outside platform approval are refused."""
    repo = _repo(server)
    tool_ids = tuple(
        _public_id(item, "tool revision not found") for item in dict.fromkeys(body.tool_ids)
    )
    model_ids = tuple(
        _public_id(item, "model revision not found") for item in dict.fromkeys(body.model_ids)
    )
    default_model_id = (
        _public_id(body.default_model_id, "model revision not found")
        if body.default_model_id
        else None
    )
    if default_model_id is not None and default_model_id not in model_ids:
        raise OctopError(
            ErrorCode.FORBIDDEN_ROLE,
            "default model must be one of the approved model revisions",
        )
    try:
        record = repo.update_capabilities(
            admin.tenant_id,
            actor_member_id=admin.member_id,
            tool_revision_ids=tool_ids,
            model_revision_ids=model_ids,
            default_model_revision_id=default_model_id,
            expected_revision=body.expected_revision,
        )
    except _CATALOG_ERRORS as exc:
        raise _capability_refusal(exc) from exc
    return workbuddy_envelope(request, _capabilities_payload(admin.tenant_id, record))

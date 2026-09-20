"""WorkBuddy workflow definition routes (relative to the /api/v1 prefix).

Tenant identity always comes from the authenticated WorkBuddy principal; the
definition is compiled by the single compiler
(:mod:`octop.infra.workbuddy.workflow_compiler`) before anything is persisted,
and the immutable version plus the workflow revision are written in one
PostgreSQL transaction so a failed save can never leave a partial version.

Concurrency is an explicit HTTP contract: ``PUT``/``activate``/``rollback``
require an ``If-Match`` header carrying the workflow's ETag.  A missing header
is 428 (:data:`ErrorCode.PRECONDITION_REQUIRED`); a stale one is 409
(:data:`ErrorCode.WF_VERSION_CONFLICT`) and nothing is written.

Ordinary members only ever receive published minimal projections (identity,
status and the published input specification); full definitions, version
history and pointers stay with the workflow creator and tenant admins, and an
invisible workflow is a uniform 404 — never 403.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPrincipal,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.repos.workbuddy_workflows import (
    WorkBuddyWorkflowError,
    WorkBuddyWorkflowRepo,
    WorkflowRecord,
    WorkflowVersionRecord,
)
from octop.infra.db.workbuddy_context import (
    WorkBuddyContextError,
    WorkBuddyDbContext,
    WorkBuddyPostgresRequiredError,
    workbuddy_transaction,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.model import RbacActor
from octop.infra.workbuddy.workflow_compiler import (
    CompiledWorkflow,
    WorkflowCompileError,
    compile_workflow_definition,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]

_WORKFLOW_NOT_FOUND = "workflow not found"
_VERSION_NOT_FOUND = "workflow version not found"
_IF_MATCH_REQUIRED = "If-Match header with the workflow ETag is required"
_IF_MATCH_STALE = "workflow revision does not match the If-Match header"
_STORE_UNAVAILABLE = "workflow store is unavailable"


class WorkflowCreateBody(BaseModel):
    """Create a draft; the definition is compiled before it is stored."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    definition: dict[str, Any]


class WorkflowSaveBody(BaseModel):
    """Append one immutable version on top of ``base_version_id``."""

    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]
    base_version_id: str | None = Field(default=None, max_length=64)
    change_summary: str | None = Field(default=None, max_length=500)


class WorkflowActivateBody(BaseModel):
    """Publish a version; proposal candidates are refused."""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)
    mode: str = Field(default="active", pattern="^(active|shadow)$")


class WorkflowRollbackBody(BaseModel):
    """Copy a historical version into a new immutable version and publish it."""

    model_config = ConfigDict(extra="forbid")

    version_id: str = Field(min_length=1, max_length=64)
    change_summary: str | None = Field(default=None, max_length=500)


class DefinitionValidateBody(BaseModel):
    """Validate and normalize a draft without persisting or activating it."""

    model_config = ConfigDict(extra="forbid")

    definition: dict[str, Any]


def _public_id(value: str, detail: str) -> str:
    """Malformed identifiers are indistinguishable from invisible ones (404)."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, detail) from exc


def _repo(server: Any) -> WorkBuddyWorkflowRepo:
    """Instantiate the workflow repo from the shared pool (it owns its transactions)."""
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo as Repo

    return Repo(server.services.db)


def _refusal(exc: Exception) -> OctopError:
    """Map a store/compiler refusal onto its stable error code; never fake success."""
    if isinstance(exc, OctopError):
        # Routes raise coded errors themselves (404 for invisible rows, 409 for a
        # bad state); those must survive unchanged, or a uniform 404 turns into a
        # 503 and leaks that the object exists but is out of reach.
        return exc
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or exc)
    if isinstance(exc, WorkBuddyPostgresRequiredError):
        return OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, message)
    if isinstance(exc, WorkBuddyContextError):
        return OctopError(ErrorCode.WF_INVALID_SCHEMA, message)
    if isinstance(exc, (WorkBuddyWorkflowError, WorkflowCompileError)):
        try:
            return OctopError(ErrorCode(code), message)
        except ValueError:
            return OctopError(ErrorCode.WF_INVALID_SCHEMA, message)
    # Anything unexpected still fails closed, but it must not disappear: an
    # opaque 503 once hid a jsonb binding error from every test.
    logger.exception("unhandled workflow store failure", exc_info=exc)
    return OctopError(ErrorCode.DEPENDENCY_UNAVAILABLE, _STORE_UNAVAILABLE)


def _etag(record: WorkflowRecord) -> str:
    """Strong validator over the compare-and-swap unit (no content hash leaks)."""
    return f'"{record.workflow_id}.{record.revision}"'


def _require_if_match(request: Request, record: WorkflowRecord) -> None:
    header = (request.headers.get("if-match") or "").strip()
    if not header:
        raise OctopError(ErrorCode.PRECONDITION_REQUIRED, _IF_MATCH_REQUIRED)
    candidates = {candidate.strip() for candidate in header.split(",")}
    if _etag(record) not in candidates:
        raise OctopError(ErrorCode.WF_VERSION_CONFLICT, _IF_MATCH_STALE)


def _is_owner(record: WorkflowRecord, principal: WorkBuddyPrincipal) -> bool:
    return record.created_by == principal.user_id or (
        record.created_by_membership_id is not None
        and record.created_by_membership_id == principal.member_id
    )


def _rbac_actor(principal: WorkBuddyPrincipal) -> RbacActor:
    """The permission model's view of the caller (same identities, no extra lookup)."""
    return RbacActor(
        user_id=int(principal.user_id),
        tenant_id=principal.tenant_id,
        department_id=principal.department_id,
        is_tenant_admin=principal.is_admin,
    )


def _reachable(
    server: Any, principal: WorkBuddyPrincipal, workflow_id: str, permission: str
) -> bool:
    """True when the workflow's permission layers reach ``permission``.

    A workflow with no permission row is company-visible for reads — the state every
    workflow was in before the model existed, and the same fallback the list query
    applies — while management of it stays with the creator and tenant admins,
    which the caller checks before asking here.
    """
    from octop.infra.db.repos.workbuddy_workflows import RBAC_OBJECT_KIND
    from octop.infra.rbac.repo import WorkBuddyRbacRepo
    from octop.infra.rbac.service import RbacService

    context = WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id)
    db = server.services.db
    scope_row = WorkBuddyRbacRepo(db).get_scope(context, RBAC_OBJECT_KIND, workflow_id)
    if scope_row is None:
        return permission == "read"
    try:
        RbacService(db).require(
            context,
            object_kind=RBAC_OBJECT_KIND,
            object_id=workflow_id,
            actor=_rbac_actor(principal),
            permission=permission,
        )
        return True
    except OctopError:
        return False


def _require_readable(server: Any, principal: WorkBuddyPrincipal, record: WorkflowRecord) -> None:
    """Reading a workflow needs a layer that reaches it; anything else is a 404."""
    if principal.is_admin or _is_owner(record, principal):
        return
    if not _reachable(server, principal, record.workflow_id, "read"):
        raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, _WORKFLOW_NOT_FOUND)


def _load_workflow(
    repo: Any, principal: WorkBuddyPrincipal, workflow_id: str, *, conn: Any | None = None
) -> WorkflowRecord:
    record: WorkflowRecord | None = repo.get_workflow(
        principal.tenant_id, _public_id(workflow_id, _WORKFLOW_NOT_FOUND), conn=conn
    )
    if record is None:
        raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, _WORKFLOW_NOT_FOUND)
    return record


def _load_managed_workflow(
    repo: Any,
    principal: WorkBuddyPrincipal,
    workflow_id: str,
    *,
    server: Any | None = None,
    conn: Any | None = None,
) -> WorkflowRecord:
    """Creator, tenant admin, or a holder of write/admin through the permission model.

    Everyone else sees a uniform 404, so an object that is out of reach stays
    indistinguishable from one that does not exist.
    """
    record = _load_workflow(repo, principal, workflow_id, conn=conn)
    if principal.is_admin or _is_owner(record, principal):
        return record
    if server is not None and _reachable(server, principal, record.workflow_id, "write"):
        return record
    raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, _WORKFLOW_NOT_FOUND)


# --------------------------------------------------------------------------- #
# projections — minimal for ordinary members, never a secret or a hash
# --------------------------------------------------------------------------- #


def _member_summary(record: WorkflowRecord) -> dict[str, Any]:
    return {
        "id": record.workflow_id,
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "updated_at": record.updated_at,
    }


def _member_detail(record: WorkflowRecord, version: WorkflowVersionRecord | None) -> dict[str, Any]:
    published_inputs: dict[str, Any] = {}
    if version is not None and isinstance(version.definition, dict):
        declared = version.definition.get("inputs")
        if isinstance(declared, dict):
            published_inputs = declared
    return {
        **_member_summary(record),
        "version_id": version.workflow_version_id if version is not None else None,
        "version_number": version.version_number if version is not None else None,
        "inputs": published_inputs,
    }


def _admin_payload(record: WorkflowRecord) -> dict[str, Any]:
    return {
        "id": record.workflow_id,
        "name": record.name,
        "description": record.description,
        "status": record.status,
        "revision": record.revision,
        "active_version_id": record.active_version_id,
        "shadow_version_id": record.shadow_version_id,
        "created_by": record.created_by,
        "created_by_membership_id": record.created_by_membership_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "archived_at": record.archived_at,
    }


def _version_payload(
    version: WorkflowVersionRecord,
    *,
    active_version_id: str | None,
    shadow_version_id: str | None,
    include_definition: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": version.workflow_version_id,
        "workflow_id": version.workflow_id,
        "version_number": version.version_number,
        "origin": version.origin,
        "change_summary": version.change_summary,
        "base_version_id": version.base_version_id,
        "source_version_id": version.source_version_id,
        "created_by": version.created_by,
        "created_at": version.created_at,
        "is_active": version.workflow_version_id == active_version_id,
        "is_shadow": version.workflow_version_id == shadow_version_id,
        "is_candidate": version.origin == "proposal",
    }
    if include_definition:
        payload["definition"] = version.definition
    return payload


def _validate_payload(compiled: CompiledWorkflow) -> dict[str, Any]:
    return {
        "valid": True,
        "definition": compiled.definition,
        "entry_node_id": compiled.entry_node_id,
        "node_count": len(compiled.nodes),
        "edge_count": len(compiled.edges),
        "exit_node_ids": list(compiled.exit_node_ids),
        "save_as": dict(compiled.output_key_by_node),
        "semantic_checks": compiled.semantic_checks,
        "compiler_version": compiled.compiler_version,
    }


# --------------------------------------------------------------------------- #
# workflow-definition validation (no persistence, no activation)
# --------------------------------------------------------------------------- #


@router.post("/workflow-definitions/validate", summary="Validate and normalize a workflow draft")
async def validate_workflow_definition(
    request: Request,
    body: DefinitionValidateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Run the one compiler with the tenant resolver; nothing is stored."""
    try:
        with workbuddy_transaction(
            server.services.db,
            WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id),
        ) as conn:
            from octop.infra.db.repos.workbuddy_workflows import (
                PostgresWorkflowSemanticResolver,
            )

            resolver = PostgresWorkflowSemanticResolver(
                conn, principal.tenant_id, user_id=principal.user_id
            )
            compiled = compile_workflow_definition(
                body.definition, resolver=resolver, require_semantic_resolution=True
            )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, _validate_payload(compiled))


# --------------------------------------------------------------------------- #
# workflows
# --------------------------------------------------------------------------- #


@router.post("/workflows", status_code=201, summary="Create a workflow draft")
async def create_workflow(
    request: Request,
    body: WorkflowCreateBody,
    principal: _Principal,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Compile the definition, then store the draft and its first version."""
    repo = _repo(server)
    try:
        with workbuddy_transaction(
            server.services.db,
            WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id),
        ) as conn:
            from octop.infra.db.repos.workbuddy_workflows import (
                PostgresWorkflowSemanticResolver,
            )

            resolver = PostgresWorkflowSemanticResolver(
                conn, principal.tenant_id, user_id=principal.user_id
            )
            compiled = compile_workflow_definition(
                body.definition, resolver=resolver, require_semantic_resolution=True
            )
            bundle = repo.create_workflow(
                principal.tenant_id,
                name=body.name,
                description=body.description,
                definition=compiled.definition,
                definition_sha256=compiled.definition_sha256,
                created_by_user_id=principal.user_id,
                created_by_membership_id=principal.member_id,
                conn=conn,
            )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    response.headers["ETag"] = _etag(bundle.workflow)
    payload = {
        **_admin_payload(bundle.workflow),
        "version": _version_payload(
            bundle.version,
            active_version_id=None,
            shadow_version_id=None,
            include_definition=True,
        ),
    }
    return workbuddy_envelope(request, payload)


@router.get("/workflows", summary="List visible workflows")
async def list_workflows(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Admins see every workflow; members see the published execution catalog."""
    repo = _repo(server)
    try:
        records = repo.list_workflows(
            principal.tenant_id,
            user_id=principal.user_id,
            department_id=principal.department_id,
            is_tenant_admin=principal.is_admin,
        )
        if not principal.is_admin:
            revoked = repo.list_revoked_workflow_ids(principal.tenant_id)
            records = [
                record
                for record in records
                if record.status == "active"
                and record.active_version_id is not None
                and record.workflow_id not in revoked
            ]
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    items = [
        _admin_payload(record) if principal.is_admin else _member_summary(record)
        for record in records
    ]
    return workbuddy_envelope(request, {"items": items})


@router.get("/workflows/{workflow_id}", summary="Workflow detail with ETag")
async def get_workflow(
    request: Request,
    workflow_id: str,
    principal: _Principal,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Managers get the definition and pointers; members get the input spec."""
    repo = _repo(server)
    try:
        record = _load_workflow(repo, principal, workflow_id)
        _require_readable(server, principal, record)
        managed = principal.is_admin or _is_owner(record, principal)
        active = repo.load_active_version(principal.tenant_id, record.workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    response.headers["ETag"] = _etag(record)
    if not managed:
        if record.status != "active" or record.active_version_id is None:
            raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, _WORKFLOW_NOT_FOUND)
        return workbuddy_envelope(request, _member_detail(record, active))
    payload = {
        **_admin_payload(record),
        "active_version": (
            _version_payload(
                active,
                active_version_id=record.active_version_id,
                shadow_version_id=record.shadow_version_id,
                include_definition=True,
            )
            if active is not None
            else None
        ),
    }
    return workbuddy_envelope(request, payload)


@router.put("/workflows/{workflow_id}", status_code=201, summary="Save a new workflow version")
async def save_workflow_version(
    request: Request,
    workflow_id: str,
    body: WorkflowSaveBody,
    principal: _Principal,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Append an immutable version; requires If-Match and the integer CAS revision."""
    repo = _repo(server)
    try:
        with workbuddy_transaction(
            server.services.db,
            WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id),
        ) as conn:
            record = _load_managed_workflow(repo, principal, workflow_id, server=server)
            _require_if_match(request, record)
            from octop.infra.db.repos.workbuddy_workflows import (
                PostgresWorkflowSemanticResolver,
            )

            resolver = PostgresWorkflowSemanticResolver(
                conn, principal.tenant_id, user_id=principal.user_id
            )
            compiled = compile_workflow_definition(
                body.definition, resolver=resolver, require_semantic_resolution=True
            )
            bundle = repo.save_version(
                principal.tenant_id,
                record.workflow_id,
                definition=compiled.definition,
                definition_sha256=compiled.definition_sha256,
                expected_revision=record.revision,
                created_by_user_id=principal.user_id,
                created_by_membership_id=principal.member_id,
                base_version_id=(
                    _public_id(body.base_version_id, _VERSION_NOT_FOUND)
                    if body.base_version_id
                    else record.active_version_id
                ),
                change_summary=body.change_summary,
                conn=conn,
            )
    except OctopError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    response.headers["ETag"] = _etag(bundle.workflow)
    payload = {
        **_admin_payload(bundle.workflow),
        "version": _version_payload(
            bundle.version,
            active_version_id=bundle.workflow.active_version_id,
            shadow_version_id=bundle.workflow.shadow_version_id,
            include_definition=True,
        ),
    }
    return workbuddy_envelope(request, payload)


@router.post("/workflows/{workflow_id}/activate", summary="Publish a workflow version")
async def activate_workflow_version(
    request: Request,
    workflow_id: str,
    body: WorkflowActivateBody,
    principal: _Principal,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Explicit publication; a proposal candidate can never be selected."""
    repo = _repo(server)
    try:
        with workbuddy_transaction(
            server.services.db,
            WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id),
        ) as conn:
            record = _load_managed_workflow(repo, principal, workflow_id, server=server)
            _require_if_match(request, record)
            updated = repo.activate_version(
                principal.tenant_id,
                record.workflow_id,
                _public_id(body.version_id, _VERSION_NOT_FOUND),
                expected_revision=record.revision,
                mode=body.mode,
                conn=conn,
            )
    except OctopError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    response.headers["ETag"] = _etag(updated)
    return workbuddy_envelope(request, _admin_payload(updated))


@router.post(
    "/workflows/{workflow_id}/rollback",
    status_code=201,
    summary="Copy a historical version and publish it",
)
async def rollback_workflow_version(
    request: Request,
    workflow_id: str,
    body: WorkflowRollbackBody,
    principal: _Principal,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Rollback copies the snapshot into a new version; history is never rewritten."""
    repo = _repo(server)
    try:
        with workbuddy_transaction(
            server.services.db,
            WorkBuddyDbContext.for_tenant(principal.tenant_id, user_id=principal.user_id),
        ) as conn:
            record = _load_managed_workflow(repo, principal, workflow_id, server=server)
            _require_if_match(request, record)
            bundle = repo.rollback_version(
                principal.tenant_id,
                record.workflow_id,
                _public_id(body.version_id, _VERSION_NOT_FOUND),
                expected_revision=record.revision,
                created_by_user_id=principal.user_id,
                created_by_membership_id=principal.member_id,
                change_summary=body.change_summary,
                conn=conn,
            )
    except OctopError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    response.headers["ETag"] = _etag(bundle.workflow)
    payload = {
        **_admin_payload(bundle.workflow),
        "version": _version_payload(
            bundle.version,
            active_version_id=bundle.workflow.active_version_id,
            shadow_version_id=bundle.workflow.shadow_version_id,
            include_definition=True,
        ),
    }
    return workbuddy_envelope(request, payload)


@router.get("/workflows/{workflow_id}/versions", summary="List immutable versions")
async def list_workflow_versions(
    request: Request,
    workflow_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Creator/admin only; members see a uniform 404."""
    repo = _repo(server)
    try:
        record = _load_managed_workflow(repo, principal, workflow_id, server=server)
        versions = repo.list_versions(principal.tenant_id, record.workflow_id)
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    items = [
        _version_payload(
            version,
            active_version_id=record.active_version_id,
            shadow_version_id=record.shadow_version_id,
            include_definition=False,
        )
        for version in versions
    ]
    return workbuddy_envelope(request, {"items": items})


@router.get("/workflows/{workflow_id}/versions/{version_id}", summary="Read one version")
async def get_workflow_version(
    request: Request,
    workflow_id: str,
    version_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """The version must belong to the workflow in the path."""
    repo = _repo(server)
    try:
        record = _load_managed_workflow(repo, principal, workflow_id, server=server)
        version = repo.get_version(
            principal.tenant_id,
            record.workflow_id,
            _public_id(version_id, _VERSION_NOT_FOUND),
        )
        if version is None:
            raise OctopError(ErrorCode.RESOURCE_NOT_FOUND, _VERSION_NOT_FOUND)
    except OctopError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _refusal(exc) from exc
    return workbuddy_envelope(
        request,
        _version_payload(
            version,
            active_version_id=record.active_version_id,
            shadow_version_id=record.shadow_version_id,
            include_definition=True,
        ),
    )

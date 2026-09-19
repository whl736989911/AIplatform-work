"""WorkBuddy template marketplace: browse, submissions, review, install, upgrade.

The frozen contract (``contracts/route-manifest.json``, base ``/api/v1``) splits
this surface in two halves, and so does the authorization:

* tenant routes (``/marketplace/…``) resolve tenant identity from the
  authenticated WorkBuddy principal — never from the body, a header or a path
  segment — and the service decides what the caller may see or do.  A submission
  is visible to its author and to tenant admins only; an installation is visible
  to whoever installed it and to tenant admins; anything else answers a uniform
  404, never 403.
* ``POST /platform/submissions/{tenant_id}/{submission_id}/decisions`` is the
  platform-management surface.  It requires an explicit ``workbuddy-platform``
  audience token and names its tenant in the path, because the reviewer is not a
  member of the tenant that submitted.

Installing a template is a two-part contract: the caller accepts the published
license and capability set (``consent``), and binds every declared slot
(``bindings``/``credential_bindings``).  The service re-derives the licence and
capability digests from the published version, so a caller that accepted a
different revision is refused (``WORKBUDDY_CONSENT_REQUIRED``) instead of
silently installing something else.  Nothing here re-implements those rules.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPlatformPrincipal,
    WorkBuddyPrincipal,
    require_platform_audience,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.repos.workbuddy_catalog import WorkBuddyCatalogRepo
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_marketplace import (
    WorkBuddyMarketplaceError,
    WorkBuddyMarketplaceRepo,
)
from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
from octop.infra.db.workbuddy_context import (
    WorkBuddyContextError,
    WorkBuddyPostgresRequiredError,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.marketplace import (
    MarketplaceError,
    MarketplaceService,
    MarketplaceStorePort,
)
from octop.infra.workbuddy.marketplace_ports import (
    CatalogCapabilityAdapter,
    TenantBindingAdapter,
    WorkflowInstallAdapter,
)

router = APIRouter()

logger = logging.getLogger(__name__)

#: The refusal types the marketplace raises on purpose: the service's sanitizer
#: and the repository's translators.  Anything else is a deployment failure.
DomainRefusal = (MarketplaceError, WorkBuddyMarketplaceError, WorkBuddyContextError)

_STORE_UNAVAILABLE = "the marketplace store is unavailable"

_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]
_Platform = Annotated[WorkBuddyPlatformPrincipal, Depends(require_platform_audience())]

MARKETPLACE_NOT_FOUND = "marketplace resource not found"
SUBMISSION_NOT_FOUND = "submission not found"
INSTALLATION_NOT_FOUND = "installation not found"

#: Domain refusal -> the frozen ErrorCode the contract publishes for it.  A code
#: that is missing here is a validation refusal, not a server fault.
_ERROR_CODES: dict[str, ErrorCode] = {
    # The contract's marketplace refusals, mapped onto the frozen code set.
    "WORKBUDDY_MARKETPLACE_NOT_FOUND": ErrorCode.WORKBUDDY_MARKETPLACE_NOT_FOUND,
    "WORKBUDDY_SUBMISSION_INVALID": ErrorCode.WORKBUDDY_SUBMISSION_INVALID,
    "WORKBUDDY_SUBMISSION_FROZEN": ErrorCode.WORKBUDDY_SUBMISSION_FROZEN,
    "WORKBUDDY_SUBMISSION_NOT_APPROVABLE": ErrorCode.WORKBUDDY_SUBMISSION_NOT_APPROVABLE,
    "WORKBUDDY_CONSENT_REQUIRED": ErrorCode.WORKBUDDY_CONSENT_REQUIRED,
    "WORKBUDDY_LICENSE_NOT_ACCEPTED": ErrorCode.WORKBUDDY_LICENSE_NOT_ACCEPTED,
    "WORKBUDDY_VERSION_IMMUTABLE": ErrorCode.WORKBUDDY_VERSION_IMMUTABLE,
    "WORKBUDDY_TEMPLATE_VERSION_NOT_INSTALLABLE": (
        ErrorCode.WORKBUDDY_TEMPLATE_VERSION_NOT_INSTALLABLE
    ),
    "WORKBUDDY_REBINDING_INCOMPLETE": ErrorCode.WORKBUDDY_REBINDING_INCOMPLETE,
    "WORKBUDDY_CAPABILITY_NOT_APPROVED": ErrorCode.WORKBUDDY_CAPABILITY_NOT_APPROVED,
    "WORKBUDDY_MODEL_NOT_CONFIGURED": ErrorCode.WORKBUDDY_MODEL_NOT_CONFIGURED,
    "WORKBUDDY_DEPENDENCY_UNAVAILABLE": ErrorCode.WORKBUDDY_DEPENDENCY_UNAVAILABLE,
    "DEPENDENCY_UNAVAILABLE": ErrorCode.WORKBUDDY_DEPENDENCY_UNAVAILABLE,
    "WORKBUDDY_INVALID_ARGUMENT": ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
    "WORKBUDDY_CONTEXT_INVALID": ErrorCode.WORKBUDDY_CONTEXT_INVALID,
    "WORKBUDDY_VALIDATION_FAILED": ErrorCode.WORKBUDDY_VALIDATION_FAILED,
    "STATE_CONFLICT": ErrorCode.STATE_CONFLICT,
}


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class ConsentBody(BaseModel):
    """The caller's acceptance of one published license + capability set."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool = Field(description="Explicit acceptance; the service refuses false.")
    template_version_id: str = Field(min_length=1, max_length=64)
    license_text_hash: str = Field(min_length=1, max_length=128)
    capabilities_hash: str = Field(min_length=1, max_length=128)


class InstallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_version_id: str = Field(min_length=1, max_length=64)
    consent: ConsentBody
    bindings: dict[str, Any] = Field(default_factory=dict)
    credential_bindings: dict[str, Any] = Field(default_factory=dict)
    workflow_name: str | None = Field(default=None, max_length=200)


class UpgradeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_version_id: str = Field(min_length=1, max_length=64)
    consent: ConsentBody
    bindings: dict[str, Any] = Field(default_factory=dict)
    credential_bindings: dict[str, Any] = Field(default_factory=dict)
    template_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "The template the caller means. The contract's upgrade body names only "
            "the target version, so this is optional; when it is present it must be "
            "the template the installation belongs to."
        ),
    )


class SubmissionCreateBody(BaseModel):
    """A sanitized template draft; the service rejects private identifiers."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    industry: str = Field(default="", max_length=64)
    definition: dict[str, Any]
    license_id: str = Field(min_length=1, max_length=64)
    license_text: str = Field(min_length=1, max_length=20000)
    capabilities: list[dict[str, Any]] = Field(default_factory=list)


class SubmissionSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int | None = Field(
        default=None,
        ge=0,
        description="Revision the author saw; the store compare-and-swaps it.",
    )


class PlatformDecisionBody(BaseModel):
    """Platform review of another tenant's submission."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    platform_review_ref: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    expected_revision: int | None = Field(default=None, ge=0)
    publication: dict[str, Any] | None = Field(
        default=None,
        description="Publishing parameters, required when the decision approves.",
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def _not_found(message: str) -> OctopError:
    return OctopError(ErrorCode.WORKBUDDY_MARKETPLACE_NOT_FOUND, message)


def _public_id(value: str) -> str:
    """Normalize a path identifier; a malformed one is indistinguishable from a miss."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return str(value)


def _refusal(exc: Exception) -> OctopError:
    """Map a store/domain refusal onto its stable code; never fake success.

    The workflow slice's router follows the same contract: a coded error passes
    through untouched, a known domain refusal keeps its published code, and
    anything unexpected is logged and answered as an unavailable dependency
    rather than an opaque 500.
    """
    if isinstance(exc, OctopError):
        return exc
    if isinstance(exc, WorkBuddyPostgresRequiredError):
        # A SQLite control plane fails closed (migration 021): the marketplace
        # tables do not exist there, so this is a deployment fact, not a request
        # error, and it answers the code the migrations document for it.
        return OctopError(ErrorCode.WORKBUDDY_POSTGRES_REQUIRED, str(exc))
    if isinstance(exc, DomainRefusal):
        code = _ERROR_CODES.get(
            str(getattr(exc, "code", "") or ""), ErrorCode.WORKBUDDY_VALIDATION_FAILED
        )
        details = dict(getattr(exc, "details", {}) or {})
        path = str(getattr(exc, "path", "") or "")
        if path:
            details.setdefault("path", path)
        details.setdefault("reason", str(getattr(exc, "code", "") or code.value))
        return OctopError(code, str(exc), details=details)
    logger.exception("unhandled marketplace store failure", exc_info=exc)
    return OctopError(ErrorCode.WORKBUDDY_DEPENDENCY_UNAVAILABLE, _STORE_UNAVAILABLE)


def _db(server: Any) -> Any:
    services = getattr(server, "services", None)
    db = getattr(services, "db", None)
    if db is None:
        raise OctopError(
            ErrorCode.SETUP_REQUIRED,
            "control-plane database not configured yet",
            status=503,
        )
    return db


def _service(server: Any) -> MarketplaceService:
    """Bind the marketplace service to the control plane's repositories.

    ``MarketplaceStorePort`` declares its methods with ``**kwargs`` (the shape a
    test double implements), while the repository spells the same keywords out —
    so the repository satisfies the port in behaviour but not structurally.  The
    cast records that gap; the route and PostgreSQL suites exercise every call
    the service makes through this binding.
    """
    db = _db(server)
    catalog = WorkBuddyCatalogRepo(db)
    identity = WorkBuddyIdentityRepo(db)
    store = cast("MarketplaceStorePort", WorkBuddyMarketplaceRepo(db))
    return MarketplaceService(
        store=store,
        catalog=CatalogCapabilityAdapter(catalog),
        bindings=TenantBindingAdapter(identity=identity, catalog=catalog, db=db),
        workflows=WorkflowInstallAdapter(WorkBuddyWorkflowRepo(db)),
        db=db,
    )


def _plan_payload(plan: Any) -> dict[str, Any]:
    return dataclasses.asdict(plan)


# --------------------------------------------------------------------------- #
# Catalogue
# --------------------------------------------------------------------------- #


@router.get("/marketplace/templates", summary="Browse reviewed public templates")
async def browse_templates(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    industry: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> dict[str, Any]:
    """Published, sanitized template metadata for the current member."""
    service = _service(server)
    try:
        rows = service.browse_templates(industry=industry, limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, rows)


@router.get(
    "/marketplace/templates/{template_id}/versions/{version_id}",
    summary="Read one published template version",
)
async def template_version(
    request: Request,
    template_id: str,
    version_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """License, required capabilities and content digest of a published version."""
    service = _service(server)
    try:
        view = service.template_version(_public_id(template_id), _public_id(version_id))
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, view)


@router.post(
    "/marketplace/templates/{template_id}/install",
    status_code=202,
    summary="Install a reviewed template",
)
async def install_template(
    request: Request,
    template_id: str,
    body: InstallBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Start an install job; the caller must have accepted the published version."""
    service = _service(server)
    try:
        outcome = service.install(
            tenant_id=principal.tenant_id,
            member_id=principal.member_id,
            user_id=principal.user_id,
            template_id=_public_id(template_id),
            template_version_id=body.template_version_id,
            consent_json=body.consent.model_dump(),
            bindings_json=body.bindings,
            credential_bindings_json=body.credential_bindings,
            workflow_name=body.workflow_name,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(
        request,
        {
            "installation": outcome.installation,
            "job": outcome.job,
            "plan": _plan_payload(outcome.plan),
        },
    )


@router.get(
    "/marketplace/installations/{installation_id}",
    summary="Installation state for this tenant",
)
async def installation_detail(
    request: Request,
    installation_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Visible to the installer and to tenant admins; anyone else sees 404."""
    service = _service(server)
    try:
        view = service.installation_detail(
            tenant_id=principal.tenant_id,
            installation_id=_public_id(installation_id),
            member_id=principal.member_id,
            is_admin=principal.is_admin,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    if view is None:
        raise _not_found(INSTALLATION_NOT_FOUND)
    return workbuddy_envelope(request, view)


@router.post(
    "/marketplace/installations/{installation_id}/upgrade",
    status_code=202,
    summary="Upgrade an installation to a named template version",
)
async def upgrade_installation(
    request: Request,
    installation_id: str,
    body: UpgradeBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Explicit target version plus renewed consent; returns the upgrade job.

    The contract's upgrade body names only the target ``template_version_id``, so
    the template comes from the installation itself: an upgrade stays inside the
    template the tenant installed, and a caller cannot point it at another one.
    """
    db = _db(server)
    installation = WorkBuddyMarketplaceRepo(db).get_installation(
        principal.tenant_id, _public_id(installation_id)
    )
    if installation is None:
        raise _not_found(INSTALLATION_NOT_FOUND)
    if body.template_id is not None and _public_id(body.template_id) != installation["template_id"]:
        # An upgrade stays inside the template the tenant installed: a caller that
        # names another one is refused, never silently redirected.
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "the upgrade names a different template than the installation",
            details={"path": "template_id"},
        )
    service = _service(server)
    try:
        outcome = service.upgrade(
            tenant_id=principal.tenant_id,
            member_id=principal.member_id,
            user_id=principal.user_id,
            is_admin=principal.is_admin,
            installation_id=_public_id(installation_id),
            template_id=installation["template_id"],
            template_version_id=body.template_version_id,
            consent_json=body.consent.model_dump(),
            bindings_json=body.bindings,
            credential_bindings_json=body.credential_bindings,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(
        request,
        {
            "installation": outcome.installation,
            "upgrade": outcome.upgrade,
            "job": outcome.job,
        },
    )


# --------------------------------------------------------------------------- #
# Submissions and platform review
# --------------------------------------------------------------------------- #


@router.post("/marketplace/submissions", status_code=201, summary="Create a submission draft")
async def create_submission(
    request: Request,
    body: SubmissionCreateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Author-scoped draft; private identifiers in the definition are refused here."""
    service = _service(server)
    try:
        view = service.create_submission(
            tenant_id=principal.tenant_id,
            member_id=principal.member_id,
            user_id=principal.user_id,
            name=body.name,
            summary=body.summary,
            industry=body.industry,
            definition=body.definition,
            license_id=body.license_id,
            license_text=body.license_text,
            capabilities=body.capabilities,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, view)


@router.get("/marketplace/submissions/{submission_id}", summary="Submission and review state")
async def submission_detail(
    request: Request,
    submission_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Visible to the author and to tenant admins; anyone else sees 404."""
    service = _service(server)
    try:
        view = service.submission_detail(
            tenant_id=principal.tenant_id,
            submission_id=_public_id(submission_id),
            member_id=principal.member_id,
            is_admin=principal.is_admin,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    if view is None:
        raise _not_found(SUBMISSION_NOT_FOUND)
    return workbuddy_envelope(request, view)


@router.post("/marketplace/submissions/{submission_id}/submit", summary="Freeze a submission")
async def submit_submission(
    request: Request,
    submission_id: str,
    body: SubmissionSubmitBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Freeze the draft for review; a frozen submission is immutable afterwards."""
    service = _service(server)
    try:
        view = service.freeze_submission(
            tenant_id=principal.tenant_id,
            submission_id=_public_id(submission_id),
            member_id=principal.member_id,
            is_admin=principal.is_admin,
            expected_revision=body.expected_revision,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    if view is None:
        raise _not_found(SUBMISSION_NOT_FOUND)
    return workbuddy_envelope(request, view)


@router.post(
    "/platform/submissions/{tenant_id}/{submission_id}/decisions",
    summary="Decide a submitted template (platform review)",
)
async def decide_platform_submission(
    request: Request,
    tenant_id: str,
    submission_id: str,
    body: PlatformDecisionBody,
    principal: _Platform,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Platform-management only: the reviewer is not a member of the submitting tenant."""
    service = _service(server)
    try:
        view = service.decide_submission(
            tenant_id=_public_id(tenant_id),
            submission_id=_public_id(submission_id),
            decision=body.decision,
            platform_review_ref=body.platform_review_ref,
            reviewer_user_id=principal.user_id,
            note=body.note,
            expected_revision=body.expected_revision,
            publication=body.publication,
        )
    except Exception as exc:  # noqa: BLE001 - a refusal must be coded, never a 500
        raise _refusal(exc) from exc
    if view is None:
        raise _not_found(SUBMISSION_NOT_FOUND)
    # The decision body is published as ``{submission, review, published_version}``
    # (MarketplaceDecisionResult): the review record and the version the approval
    # published are separate facts from the submission row itself.
    submission = {
        key: value for key, value in view.items() if key not in ("review", "published_version")
    }
    return workbuddy_envelope(
        request,
        {
            "submission": submission,
            "review": view.get("review"),
            "published_version": view.get("published_version"),
        },
    )


__all__ = ["router"]

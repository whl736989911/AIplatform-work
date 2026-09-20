"""HTTP API for WorkBuddy improvement proposals.

Paths are relative to the parent-mounted ``/api/v1`` prefix:

* ``POST /workflows/{workflow_id}/improvement-proposals`` — compile a patch
  against the workflow's current (fixed) base version and open a proposal;
* ``GET  /improvement-proposals`` — list visible proposals;
* ``GET  /improvement-proposals/{id}`` — detail with reviews, shadow proof and
  gate evidence;
* ``POST /improvement-proposals/{id}/decisions`` — record an independent review;
* ``POST /improvement-proposals/{id}/reviewers`` — the workflow manager assigns
  the reviewers of an open proposal;
* ``POST /improvement-proposals/{id}/promote`` — ``start_shadow``,
  ``start_canary``, ``apply`` or ``abort`` with an ``If-Match`` workflow
  revision.

Cross-tenant and otherwise invisible objects are indistinguishable: a foreign
proposal id answers exactly like an unknown one.  No response ever carries a
secret, credential, hash of a secret or internal encrypted payload.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from octop.api.deps import get_server
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPrincipal,
    require_workbuddy_admin,
    workbuddy_envelope,
    workbuddy_principal,
)
from octop.infra.db.repos.workbuddy_catalog import WorkBuddyCatalogRepo
from octop.infra.db.repos.workbuddy_identity import WorkBuddyError
from octop.infra.db.repos.workbuddy_proposals import (
    MAX_LIST_LIMIT,
    WorkBuddyProposalsRepo,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.proposals import (
    MAX_ASSIGNED_REVIEWERS,
    MAX_PATCH_OPERATIONS,
    PromotionAction,
    ProposalActor,
    ProposalNotFoundError,
    ProposalPolicy,
    ProposalPolicyError,
    ProposalStatus,
    ProposalView,
    ReviewDecision,
    WorkBuddyProposalsService,
)
from octop.infra.workbuddy.runtime import (
    RuntimeCanaryMetrics,
    RuntimeJobRecorder,
    RuntimeShadowRunner,
)

router = APIRouter()

_require_admin = require_workbuddy_admin()
_Principal = Annotated[WorkBuddyPrincipal, Depends(workbuddy_principal)]
_Admin = Annotated[WorkBuddyPrincipal, Depends(_require_admin)]

_IF_MATCH_RE = re.compile(r'^(?:W/)?"?([0-9]+)"?$')

_PROPOSAL_NOT_FOUND = "improvement proposal not found"

# Domain code -> HTTP error code.  Unmapped codes fall back to a validation
# failure so a client never sees an internal error for a policy refusal.
_ERROR_CODES: dict[str, ErrorCode] = {
    "TRIGGER_CHANGE": ErrorCode.PROPOSAL_GOVERNANCE_REQUIRED,
    "APPROVER_CHANGE": ErrorCode.PROPOSAL_GOVERNANCE_REQUIRED,
    "TARGET_CHANGE": ErrorCode.PROPOSAL_GOVERNANCE_REQUIRED,
    "AUTH_BOUNDARY_CHANGE": ErrorCode.PROPOSAL_GOVERNANCE_REQUIRED,
    "APPROVAL_BYPASS": ErrorCode.PROPOSAL_GOVERNANCE_REQUIRED,
    "UNAPPROVED_TOOL": ErrorCode.FORBIDDEN_ROLE,
    "PRIVATE_ID": ErrorCode.WORKBUDDY_VALIDATION_FAILED,
    "INVALID_DEFINITION": ErrorCode.WF_INVALID_SCHEMA,
    "INVALID_PATCH": ErrorCode.PROPOSAL_PATCH_INVALID,
    "NO_SEMANTIC_CHANGE": ErrorCode.PROPOSAL_PATCH_INVALID,
    "ALLOWLIST_UNAVAILABLE": ErrorCode.DEPENDENCY_UNAVAILABLE,
    "TOOL_ALLOWLIST_UNAVAILABLE": ErrorCode.DEPENDENCY_UNAVAILABLE,
    "SCHEMA_UNAVAILABLE": ErrorCode.DEPENDENCY_UNAVAILABLE,
    "CREATOR_SELF_REVIEW": ErrorCode.FORBIDDEN_NOT_APPROVER,
    "DUPLICATE_REVIEW": ErrorCode.APPROVAL_ALREADY_DECIDED,
    "APPROVAL_ALREADY_DECIDED": ErrorCode.APPROVAL_ALREADY_DECIDED,
    "INVALID_APPROVAL_REQUIREMENT": ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
    "CANARY_RATIO_INVALID": ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
    "INVALID_EVALUATION": ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
    "PROPOSAL_ALREADY_OPEN": ErrorCode.PROPOSAL_CANDIDATE_EXISTS,
    "PROPOSAL_CANDIDATE_EXISTS": ErrorCode.PROPOSAL_CANDIDATE_EXISTS,
    "PROPOSAL_BASE_CONFLICT": ErrorCode.PROPOSAL_BASE_CONFLICT,
    "PROPOSAL_STALE": ErrorCode.PROPOSAL_BASE_CONFLICT,
    "STALE_REVISION": ErrorCode.PROPOSAL_BASE_CONFLICT,
    "PROPOSAL_CHANGED": ErrorCode.STATE_CONFLICT,
    "INVALID_STATE": ErrorCode.STATE_CONFLICT,
    "STATE_CONFLICT": ErrorCode.STATE_CONFLICT,
    "SHADOW_PROOF_REQUIRED": ErrorCode.PROPOSAL_GATE_NOT_MET,
    "GATES_NOT_PASSED": ErrorCode.PROPOSAL_GATE_NOT_MET,
    # A refusal that already speaks the contract's own code stays that code.
    "PROPOSAL_GATE_NOT_MET": ErrorCode.PROPOSAL_GATE_NOT_MET,
    "PROPOSAL_REVIEW_REQUIREMENT": ErrorCode.PROPOSAL_REVIEW_REQUIREMENT,
    "RECONCILIATION_REQUIRED": ErrorCode.RECONCILIATION_REQUIRED,
    "WORKFLOW_NOT_FOUND": ErrorCode.RESOURCE_NOT_FOUND,
    "DEPENDENCY_UNAVAILABLE": ErrorCode.DEPENDENCY_UNAVAILABLE,
    "WORKBUDDY_CONTEXT_INVALID": ErrorCode.WORKBUDDY_CONTEXT_INVALID,
    "WORKBUDDY_INVALID_ARGUMENT": ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
    "FORBIDDEN_ROLE": ErrorCode.FORBIDDEN_ROLE,
    "WF_INVALID_SCHEMA": ErrorCode.WF_INVALID_SCHEMA,
    "WORKBUDDY_VERSION_IMMUTABLE": ErrorCode.WORKBUDDY_VERSION_IMMUTABLE,
}

# Governance columns only the tenant administration sees.  A reviewer sees the
# frozen evidence, never the roster: an independent review must not be coloured
# by who else was assigned.
_REVIEWER_HIDDEN_FIELDS = ("reviews", "reviewers")


class ProposalCreateBody(BaseModel):
    """RFC 6902 patch against the server-fixed base version."""

    workflow_revision: int = Field(
        ge=0, description="Workflow revision the patch was drafted from."
    )
    patch: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=MAX_PATCH_OPERATIONS,
        description="RFC 6902 operations applied to the canonical base definition.",
    )
    change_summary: str = Field(default="", max_length=500)


class DecisionBody(BaseModel):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=2000)


class ReviewersBody(BaseModel):
    """The members the workflow manager designates as independent reviewers."""

    reviewer_membership_ids: list[str] = Field(
        min_length=1,
        max_length=MAX_ASSIGNED_REVIEWERS,
        description=(
            "Membership UUIDs of this tenant.  The creator of the proposal and "
            "members that are not active are refused."
        ),
    )


class PromoteBody(BaseModel):
    action: Literal["start_shadow", "start_canary", "apply", "abort"]
    ratio_basis_points: int = Field(
        default=0,
        ge=0,
        le=10_000,
        description="Candidate share for start_canary, in basis points.",
    )


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def _not_found() -> OctopError:
    """A uniform 404 for unknown, malformed, foreign and invisible proposal ids."""
    return OctopError(ErrorCode.RESOURCE_NOT_FOUND, _PROPOSAL_NOT_FOUND)


def _public_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise _not_found() from exc


def _refusal(exc: Exception) -> OctopError:
    code = _ERROR_CODES.get(str(getattr(exc, "code", "")), ErrorCode.WORKBUDDY_VALIDATION_FAILED)
    details = dict(getattr(exc, "details", {}) or {})
    details.setdefault(
        "reason", str(getattr(exc, "code", "") or ErrorCode.WORKBUDDY_VALIDATION_FAILED)
    )
    return OctopError(code, str(exc), details=details)


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


def _repo(server: Any, principal: WorkBuddyPrincipal) -> WorkBuddyProposalsRepo:
    return WorkBuddyProposalsRepo(_db(server), principal)


def _approved_tool_keys(server: Any, tenant_id: str) -> frozenset[str]:
    """Tool keys the tenant may run, resolved from its published capability grants."""
    catalog = WorkBuddyCatalogRepo(_db(server))
    capabilities = catalog.get_capabilities(tenant_id)
    if capabilities is None:
        return frozenset()
    revisions = {row.tool_revision_id: row for row in catalog.list_tool_revisions()}
    keys = {
        revisions[revision_id].tool_key
        for revision_id in capabilities.tool_revision_ids
        if revision_id in revisions and revisions[revision_id].status == "published"
    }
    return frozenset(keys)


def _service(server: Any, principal: WorkBuddyPrincipal) -> WorkBuddyProposalsService:
    """Build the service; the tenant allowlist and private-id set come from the server."""
    approved = _approved_tool_keys(server, principal.tenant_id)
    policy = ProposalPolicy(
        approved_tools=approved,
        private_ids=frozenset({principal.tenant_id, principal.member_id} - {""}),
    )
    return WorkBuddyProposalsService(
        _repo(server, principal),
        policy=policy,
        # The gates are judged on the executions this tenant actually ran, and
        # the shadow phase replays this tenant's recordings.
        metrics=RuntimeCanaryMetrics(_db(server), principal.tenant_id),
        shadow=RuntimeShadowRunner(_db(server), principal.tenant_id),
    )


def _job_recorder(server: Any, principal: WorkBuddyPrincipal) -> Any:
    """The tenant's job facts, which is where a generation operation belongs."""
    return RuntimeJobRecorder(_db(server), principal.tenant_id, principal.user_id)


def _actor(principal: WorkBuddyPrincipal) -> ProposalActor:
    return ProposalActor(
        user_id=principal.user_id,
        membership_id=principal.member_id or None,
        is_admin=principal.is_admin,
    )


def _payload(view: ProposalView, principal: WorkBuddyPrincipal) -> dict[str, Any]:
    data = view.to_dict()
    if not principal.is_admin:
        # Reviewers get the review projection: the frozen diff and gate evidence,
        # never other reviewers' identities or membership bookkeeping.
        for field in _REVIEWER_HIDDEN_FIELDS:
            data.pop(field, None)
    return data


def _maybe_status(value: str | None) -> ProposalStatus | None:
    if value is None or not value.strip():
        return None
    try:
        return ProposalStatus(value.strip())
    except ValueError as exc:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "unknown proposal status filter"
        ) from exc


def _if_match_revision(request: Request) -> int:
    raw = (request.headers.get("if-match") or "").strip()
    match = _IF_MATCH_RE.match(raw)
    if match is None:
        raise OctopError(
            ErrorCode.PRECONDITION_REQUIRED,
            "If-Match with the current workflow revision is required to promote a proposal",
        )
    return int(match.group(1))


# --------------------------------------------------------------------------- #
# Proposal lifecycle
# --------------------------------------------------------------------------- #


@router.post(
    "/workflows/{workflow_id}/improvement-proposals",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Compile a workflow improvement proposal",
)
async def create_improvement_proposal(
    request: Request,
    workflow_id: str,
    body: ProposalCreateBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> JSONResponse:
    """Fix base version/hash and the immutable candidate, then open the proposal.

    The patch is applied server-side to the canonical base and re-diffed, so the
    submitted operations are never the source of truth.  Refusals (boundary,
    tool, private id, schema) answer 4xx and store nothing.
    """
    public_workflow = _public_id(workflow_id)
    service = _service(server, principal)
    # Generation is a job (contract §4.6.2): a client that lost this response can
    # find the operation under ``GET /jobs``, and the job id it is given is the
    # job's own id rather than a re-labelled proposal id.
    jobs = _job_recorder(server, principal)
    job_id = jobs.start(
        kind="improvement_proposal",
        request={
            "workflow_id": public_workflow,
            "workflow_revision": body.workflow_revision,
            "change_summary": body.change_summary,
        },
    )
    try:
        view = service.create(
            workflow_id=public_workflow,
            patch=body.patch,
            change_summary=body.change_summary,
            actor=_actor(principal),
            expect_revision=body.workflow_revision,
        )
    except ProposalPolicyError as exc:
        jobs.finish(job_id, status="failed", error_code=exc.code, error_message=exc.message)
        raise _refusal(exc) from exc
    except ProposalNotFoundError as exc:
        jobs.finish(
            job_id, status="failed", error_code="WORKFLOW_NOT_FOUND", error_message=str(exc)
        )
        raise _not_found() from exc
    except WorkBuddyError as exc:
        jobs.finish(job_id, status="failed", error_code=exc.code, error_message=exc.message)
        raise _refusal(exc) from exc
    record = view.proposal
    jobs.finish(
        job_id,
        status="succeeded",
        result={"proposal_id": record.proposal_id, "workflow_id": record.workflow_id},
    )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=workbuddy_envelope(
            request,
            {
                "job_id": job_id,
                "proposal_id": record.proposal_id,
                "workflow_id": record.workflow_id,
                "status": record.status.value,
                "risk_level": record.risk_level,
                "required_approvals": record.required_approvals,
                "requires_manual_shadow": record.requires_manual_shadow,
            },
        ),
    )


@router.get("/improvement-proposals", summary="List visible improvement proposals")
async def list_improvement_proposals(
    request: Request,
    principal: _Principal,
    server: Any = Depends(get_server),
    workflow_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=MAX_LIST_LIMIT),
) -> dict[str, Any]:
    """Tenant-scoped list; reviewers see the review projection, admins everything."""
    service = _service(server, principal)
    try:
        views = service.list(
            workflow_id=_public_id(workflow_id) if workflow_id else None,
            status=_maybe_status(status_filter),
            limit=limit,
        )
    except ProposalNotFoundError as exc:
        raise _not_found() from exc
    except WorkBuddyError as exc:
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, {"items": [_payload(view, principal) for view in views]})


@router.get("/improvement-proposals/{proposal_id}", summary="Read an improvement proposal")
async def get_improvement_proposal(
    request: Request,
    proposal_id: str,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Detail: candidate version id, risk gates, reviews, shadow proof and gates."""
    service = _service(server, principal)
    try:
        view = service.get(_public_id(proposal_id))
    except ProposalNotFoundError as exc:
        raise _not_found() from exc
    except WorkBuddyError as exc:
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, _payload(view, principal))


@router.post("/improvement-proposals/{proposal_id}/decisions", summary="Record a review decision")
async def decide_improvement_proposal(
    request: Request,
    proposal_id: str,
    body: DecisionBody,
    principal: _Principal,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Independent human review: the creator can never vote and rejects win."""
    service = _service(server, principal)
    try:
        view = service.decide(
            _public_id(proposal_id),
            reviewer=_actor(principal),
            decision=ReviewDecision(body.decision),
            comment=body.comment,
        )
    except ProposalPolicyError as exc:
        raise _refusal(exc) from exc
    except ProposalNotFoundError as exc:
        raise _not_found() from exc
    except WorkBuddyError as exc:
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, _payload(view, principal))


@router.post(
    "/improvement-proposals/{proposal_id}/reviewers",
    summary="Assign independent reviewers",
)
async def assign_improvement_proposal_reviewers(
    request: Request,
    proposal_id: str,
    body: ReviewersBody,
    principal: _Admin,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Designate the reviewers of an open proposal (workflow manager only).

    Every designated member must be active in this tenant and must not be the
    author: the creator can neither vote nor be assigned to vote.  The roster
    answers 400 otherwise, and one refusal covers foreign, suspended and unknown
    memberships alike.
    """
    service = _service(server, principal)
    try:
        view = service.assign_reviewers(
            _public_id(proposal_id),
            reviewers=body.reviewer_membership_ids,
            actor=_actor(principal),
        )
    except ProposalPolicyError as exc:
        raise _refusal(exc) from exc
    except ProposalNotFoundError as exc:
        raise _not_found() from exc
    except WorkBuddyError as exc:
        raise _refusal(exc) from exc
    return workbuddy_envelope(request, _payload(view, principal))


@router.post("/improvement-proposals/{proposal_id}/promote", summary="Promote a proposal")
async def promote_improvement_proposal(
    request: Request,
    proposal_id: str,
    body: PromoteBody,
    principal: _Admin,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Advance ``start_shadow``/``start_canary``/``apply``/``abort`` for the tenant admin.

    ``If-Match`` carries the workflow revision the admin saw; the store
    compare-and-swaps it, so a racing save/rollback refuses this promotion
    instead of overwriting it.  The response ETag is the resulting revision.
    """
    public_id = _public_id(proposal_id)
    if_match = _if_match_revision(request)
    service = _service(server, principal)
    try:
        view = service.promote(
            public_id,
            action=PromotionAction(body.action),
            if_match_revision=if_match,
            actor=_actor(principal),
            ratio_basis_points=body.ratio_basis_points,
        )
    except ProposalPolicyError as exc:
        raise _refusal(exc) from exc
    except ProposalNotFoundError as exc:
        raise _not_found() from exc
    except WorkBuddyError as exc:
        raise _refusal(exc) from exc
    pointer = _repo(server, principal).workflow_pointer(view.proposal.workflow_id)
    revision = pointer.revision if pointer is not None else view.proposal.workflow_revision
    response.headers["ETag"] = f'W/"{revision}"'
    payload = workbuddy_envelope(request, _payload(view, principal))
    payload["workflow_revision"] = revision
    return payload


__all__ = [
    "DecisionBody",
    "PromoteBody",
    "ProposalCreateBody",
    "ReviewersBody",
    "assign_improvement_proposal_reviewers",
    "create_improvement_proposal",
    "decide_improvement_proposal",
    "get_improvement_proposal",
    "list_improvement_proposals",
    "promote_improvement_proposal",
    "router",
]

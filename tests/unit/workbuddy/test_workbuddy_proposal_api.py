"""Focused unit tests for the WorkBuddy proposals API router (no live PostgreSQL).

The router is driven through a minimal HTTP app with a fake proposal store and a
fake catalog, so authorization, uniform 404s, the review projection and the
If-Match promotion contract are proven without a database.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.deps import get_server
from octop.api.routers import workbuddy_proposals as api
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal, workbuddy_principal
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from octop.infra.workbuddy import proposals as P

TENANT = "11111111-1111-4111-8111-111111111111"
CREATOR_MEMBER = "22222222-2222-4222-8222-222222222222"
ADMIN_MEMBER = "33333333-3333-4333-8333-333333333333"
WORKFLOW_ID = "44444444-4444-4444-8444-444444444444"
REVISION = 7
APPROVER = "55555555-5555-4555-8555-555555555555"


def workflow_definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {},
        "nodes": [
            {
                "id": "start",
                "type": "llm",
                "name": "Start",
                "config": {"prompt": "summarise", "model": "m"},
            },
            {
                "id": "fetch",
                "type": "tool",
                "name": "Fetch",
                "config": {"tool_name": "web_search", "parameters": {}},
            },
            {
                "id": "gate",
                "type": "approval",
                "name": "Gate",
                "config": {"approval_message": "ok?", "approver_user_ids": [APPROVER]},
            },
            {
                "id": "end",
                "type": "transform",
                "name": "End",
                "config": {"input": {}, "expression": "1"},
            },
        ],
        "edges": [
            {"from": "start", "to": "fetch"},
            {"from": "fetch", "to": "gate"},
            {"from": "gate", "to": "end"},
        ],
        "limits": {},
        "output": {"format": "json", "destination": "user"},
    }


class _ToolRevision:
    def __init__(self, revision_id: str, tool_key: str, status: str = "published") -> None:
        self.tool_revision_id = revision_id
        self.tool_key = tool_key
        self.status = status


class _Catalog:
    """Approved tool catalog stand-in: web_search is granted and published."""

    def __init__(self) -> None:
        self.revision_id = str(uuid.uuid4())

    def get_capabilities(self, tenant_id: str) -> Any:
        return SimpleNamespace(tool_revision_ids=(self.revision_id,))

    def list_tool_revisions(self) -> list[_ToolRevision]:
        return [_ToolRevision(self.revision_id, "web_search")]


class _Store:
    """Compact in-memory proposal store with compare-and-swap semantics."""

    def __init__(self, definition: Mapping[str, Any], *, revision: int = REVISION) -> None:
        self.definition = dict(definition)
        self.revision = revision
        self.hash = P.definition_hash(definition)
        self.proposals: dict[str, P.ProposalRecord] = {}
        self.reviews: dict[str, list[P.ReviewRecord]] = {}
        self.shadow: dict[str, list[P.ShadowRunRow]] = {}
        self.evaluations: dict[str, list[P.EvaluationRow]] = {}
        self.counter = 0
        self.reviewer_membership = "66666666-6666-4666-8666-666666666666"

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    # -- writes ------------------------------------------------------------ #

    def create_proposal(self, request: P.NewProposal, *, compile: P.CompileFn) -> P.ProposalRecord:
        if request.expect_revision != self.revision:
            raise P.ProposalConflictError("PROPOSAL_BASE_CONFLICT", "workflow revision mismatch")
        if any(
            row.workflow_id == request.workflow_id and row.status in P.PENDING_STATUSES
            for row in self.proposals.values()
        ):
            raise P.ProposalConflictError("PROPOSAL_CANDIDATE_EXISTS", "pending proposal exists")
        compiled = compile(self.definition)
        record = P.ProposalRecord(
            proposal_id=str(uuid.uuid4()),
            workflow_id=request.workflow_id,
            workflow_revision=self.revision,
            base_version_id=str(uuid.uuid4()),
            base_content_hash=compiled.base_content_hash,
            candidate_version_id=str(uuid.uuid4()),
            candidate_content_hash=compiled.candidate_content_hash,
            status=P.ProposalStatus.PENDING,
            risk_level=compiled.risk.level,
            pii_involved=compiled.risk.pii,
            required_approvals=compiled.risk.required_approvals,
            requires_manual_shadow=compiled.risk.requires_manual_shadow,
            change_summary=request.change_summary,
            changes=compiled.changes,
            created_by_user_id=request.actor.user_id,
            created_by_membership_id=request.actor.membership_id or CREATOR_MEMBER,
            created_at=1_700_000_000,
            updated_at=1_700_000_000,
        )
        self.proposals[record.proposal_id] = record
        return record

    def add_review(self, review: P.NewReview) -> P.ReviewRecord:
        rows = self.reviews.setdefault(review.proposal_id, [])
        if any(row.reviewer_user_id == review.reviewer_user_id for row in rows):
            raise P.ProposalConflictError("APPROVAL_ALREADY_DECIDED", "already voted")
        row = P.ReviewRecord(
            review_id=str(uuid.uuid4()),
            proposal_id=review.proposal_id,
            reviewer_user_id=review.reviewer_user_id,
            reviewer_membership_id=review.reviewer_membership_id,
            decision=review.decision,
            comment=review.comment,
            created_at=review.created_at,
        )
        rows.append(row)
        return row

    def add_shadow_run(self, proposal_id: str, run: P.ShadowRunRow) -> P.ShadowRunRow:
        self.shadow.setdefault(proposal_id, []).append(run)
        return run

    def add_evaluation(
        self, proposal_id: str, evaluation: P.NewEvaluation, verdict: P.GateVerdict
    ) -> P.EvaluationRow:
        row = P.EvaluationRow(
            evaluation_id=str(uuid.uuid4()),
            phase=evaluation.phase,
            window_start=evaluation.window_start,
            window_end=evaluation.window_end,
            baseline=evaluation.baseline,
            candidate=evaluation.candidate,
            verdict=verdict,
            created_at=evaluation.created_at,
        )
        self.evaluations.setdefault(proposal_id, []).append(row)
        return row

    def transition(
        self,
        proposal_id: str,
        *,
        expect_status: P.ProposalStatus,
        expect_revision: int,
        status: P.ProposalStatus,
        fields: Mapping[str, Any],
    ) -> P.ProposalRecord | None:
        record = self.proposals.get(proposal_id)
        if (
            record is None
            or record.status is not expect_status
            or record.workflow_revision != expect_revision
        ):
            return None
        updated = replace(record, status=status, updated_at=record.updated_at + 1, **fields)
        self.proposals[proposal_id] = updated
        return updated

    def apply_promotion(
        self, proposal_id: str, *, expect_revision: int, actor_user_id: int
    ) -> P.ProposalRecord | None:
        record = self.proposals.get(proposal_id)
        if record is None or record.status is not P.ProposalStatus.CANARY:
            return None
        if expect_revision != self.revision:
            return None
        self.revision += 1
        self.hash = P.definition_hash({"applied": proposal_id})
        updated = replace(
            record,
            status=P.ProposalStatus.APPLIED,
            applied_version_id=str(uuid.uuid4()),
            canary_stopped_at=record.updated_at + 1,
            canary_stop_reason="applied",
            updated_at=record.updated_at + 1,
        )
        self.proposals[proposal_id] = updated
        return updated

    # -- reads ------------------------------------------------------------- #

    def get_proposal(self, proposal_id: str) -> P.ProposalRecord | None:
        return self.proposals.get(proposal_id)

    def list_proposals(
        self,
        *,
        workflow_id: str | None = None,
        status: P.ProposalStatus | None = None,
        limit: int = 100,
    ) -> list[P.ProposalRecord]:
        rows = [
            row
            for row in self.proposals.values()
            if (workflow_id is None or row.workflow_id == workflow_id)
            and (status is None or row.status is status)
        ]
        return rows[:limit]

    def list_reviews(self, proposal_id: str) -> list[P.ReviewRecord]:
        return list(self.reviews.get(proposal_id, ()))

    def list_shadow_runs(self, proposal_id: str) -> list[P.ShadowRunRow]:
        return list(self.shadow.get(proposal_id, ()))

    def list_evaluations(self, proposal_id: str) -> list[P.EvaluationRow]:
        return list(self.evaluations.get(proposal_id, ()))

    def workflow_pointer(self, workflow_id: str) -> P.WorkflowPointer:
        return P.WorkflowPointer(
            workflow_id=workflow_id,
            revision=self.revision,
            active_version_id="version-base",
            active_definition_hash=self.hash,
        )

    def active_canary(self, workflow_id: str) -> P.ProposalRecord | None:
        return None


def _principal(
    *, user_id: int = 10, role: str = "member", member_id: str = CREATOR_MEMBER
) -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"user{user_id}", role=Role.USER, display_name=None),
        tenant_id=TENANT,
        tenant_slug="acme",
        tenant_name="Acme",
        member_id=member_id,
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


def _app(store: _Store, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(api, "WorkBuddyProposalsRepo", lambda _db, _ctx: store)
    monkeypatch.setattr(api, "WorkBuddyCatalogRepo", lambda _db: _Catalog())
    server = SimpleNamespace(services=SimpleNamespace(db=object()))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")
    app.dependency_overrides[get_server] = lambda: server

    @app.exception_handler(OctopError)
    async def _octop_error(_request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    return app


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    principal: WorkBuddyPrincipal,
    admin: WorkBuddyPrincipal | None = None,
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    app.dependency_overrides[workbuddy_principal] = lambda: principal
    app.dependency_overrides[api._require_admin] = lambda: admin or principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(
            method, f"/api/v1{path}", json=json_body, headers=dict(headers or {})
        )


CREATE_PATH = f"/workflows/{WORKFLOW_ID}/improvement-proposals"
MEDIUM_PATCH = [{"op": "replace", "path": "/nodes/0/config/prompt", "value": "be terse"}]


async def test_create_fixes_the_base_and_reports_governance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)

    response = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH, "change_summary": "tune"},
    )

    assert response.status_code == 202
    payload = response.json()["data"]
    assert payload["status"] == "pending"
    assert payload["required_approvals"] == 2
    record = store.proposals[payload["proposal_id"]]
    assert record.base_content_hash == P.definition_hash(workflow_definition())
    assert record.candidate_content_hash != record.base_content_hash


async def test_create_refuses_a_trigger_change_without_storing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)

    response = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={
            "workflow_revision": REVISION,
            "patch": [
                {
                    "op": "replace",
                    "path": "/trigger",
                    "value": {"type": "event", "config": {"event_type": "upload"}},
                }
            ],
        },
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "PROPOSAL_GOVERNANCE_REQUIRED"
    assert error["details"]["reason"] == "TRIGGER_CHANGE"
    assert store.proposals == {}


async def test_unknown_and_foreign_proposals_are_indistinguishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)

    malformed = await _request(
        app, "GET", "/improvement-proposals/not-a-uuid", principal=_principal()
    )
    missing = await _request(
        app, "GET", f"/improvement-proposals/{uuid.uuid4()}", principal=_principal()
    )

    assert malformed.status_code == missing.status_code == 404
    assert (
        malformed.json()["error"]["code"] == missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    )
    assert malformed.json()["error"]["message"] == missing.json()["error"]["message"]


async def test_creator_cannot_decide_own_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)
    created = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH},
    )
    proposal_id = created.json()["data"]["proposal_id"]

    response = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/decisions",
        principal=_principal(),
        json_body={"decision": "approved", "comment": "self"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_NOT_APPROVER"


async def test_member_listing_hides_reviewer_governance(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)
    created = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH},
    )
    proposal_id = created.json()["data"]["proposal_id"]
    store.add_review(
        P.NewReview(
            proposal_id=proposal_id,
            reviewer_user_id=20,
            reviewer_membership_id=store.reviewer_membership,
            decision=P.ReviewDecision.APPROVED,
            comment="fine",
            created_at=1_700_000_100,
        )
    )

    member = await _request(app, "GET", "/improvement-proposals", principal=_principal(user_id=30))
    admin = await _request(
        app,
        "GET",
        "/improvement-proposals",
        principal=_principal(user_id=31, role="admin", member_id=ADMIN_MEMBER),
    )

    assert "reviews" not in member.json()["data"]["items"][0]
    assert admin.json()["data"]["items"][0]["reviews"][0]["comment"] == "fine"


async def test_promote_requires_if_match_and_rejects_a_stale_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)
    created = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH},
    )
    proposal_id = created.json()["data"]["proposal_id"]
    admin = _principal(user_id=99, role="admin", member_id=ADMIN_MEMBER)

    missing = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=_principal(),
        admin=admin,
        json_body={"action": "start_shadow"},
    )
    stale = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=_principal(),
        admin=admin,
        headers={"If-Match": f'W/"{REVISION - 1}"'},
        json_body={"action": "start_shadow"},
    )

    assert missing.status_code == 428
    assert missing.json()["error"]["code"] == "PRECONDITION_REQUIRED"
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PROPOSAL_BASE_CONFLICT"


async def test_promotion_flow_enforces_shadow_and_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)
    creator = _principal()
    admin = _principal(user_id=99, role="admin", member_id=ADMIN_MEMBER)
    created = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=creator,
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH},
    )
    proposal_id = created.json()["data"]["proposal_id"]
    for reviewer in (20, 21):
        decided = await _request(
            app,
            "POST",
            f"/improvement-proposals/{proposal_id}/decisions",
            principal=_principal(user_id=reviewer, member_id=uuid.uuid4().hex),
            json_body={"decision": "approved", "comment": ""},
        )
        assert decided.status_code == 200
    assert decided.json()["data"]["status"] == "approved"

    shadow = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=creator,
        admin=admin,
        headers={"If-Match": f'W/"{REVISION}"'},
        json_body={"action": "start_shadow"},
    )
    blocked = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=creator,
        admin=admin,
        headers={"If-Match": f'W/"{REVISION}"'},
        json_body={"action": "start_canary", "ratio_basis_points": 1000},
    )
    apply_blocked = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=creator,
        admin=admin,
        headers={"If-Match": f'W/"{REVISION}"'},
        json_body={"action": "apply"},
    )

    assert shadow.status_code == 200
    assert shadow.headers["ETag"] == f'W/"{REVISION}"'
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PROPOSAL_GATE_NOT_MET"
    assert blocked.json()["error"]["details"]["reason"] == "SHADOW_PROOF_REQUIRED"
    # apply is refused before candidate traffic ran at all (invalid state), and
    # would still be refused by the gate afterwards.
    assert apply_blocked.status_code in (409,)
    assert apply_blocked.json()["error"]["code"] in ("STATE_CONFLICT", "PROPOSAL_GATE_NOT_MET")

    for index in range(P.SHADOW_MIN_SETTLED_RUNS):
        store.add_shadow_run(
            proposal_id,
            P.ShadowRunRow(
                run_id=f"shadow-{index}",
                settled=True,
                replay_only=True,
                live_side_effects=0,
                evidence_hash="a" * 64,
                created_at=1_700_000_200,
            ),
        )
    canary = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=creator,
        admin=admin,
        headers={"If-Match": f'W/"{REVISION}"'},
        json_body={"action": "start_canary", "ratio_basis_points": 1000},
    )

    assert canary.status_code == 200
    assert canary.json()["data"]["status"] == "canary"

    aborted = await _request(
        app,
        "POST",
        f"/improvement-proposals/{proposal_id}/promote",
        principal=creator,
        admin=admin,
        headers={"If-Match": f'W/"{REVISION}"'},
        json_body={"action": "abort"},
    )

    assert aborted.status_code == 200
    assert aborted.json()["data"]["status"] == "rolled_back"


async def test_promote_requires_tenant_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _Store(workflow_definition())
    app = _app(store, monkeypatch)
    created = await _request(
        app,
        "POST",
        CREATE_PATH,
        principal=_principal(),
        json_body={"workflow_revision": REVISION, "patch": MEDIUM_PATCH},
    )
    proposal_id = created.json()["data"]["proposal_id"]

    async def _deny() -> WorkBuddyPrincipal:
        raise OctopError(ErrorCode.FORBIDDEN, "admin required")

    app.dependency_overrides[api._require_admin] = _deny
    app.dependency_overrides[workbuddy_principal] = lambda: _principal()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/api/v1/improvement-proposals/{proposal_id}/promote",
            headers={"If-Match": f'W/"{REVISION}"'},
            json={"action": "abort"},
        )

    assert response.status_code == 403

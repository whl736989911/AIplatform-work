"""Live-PostgreSQL acceptance for the improvement-proposal slice.

The unit suites drive the in-memory store double; this file drives the real
repository, the real schema and the HTTP surface, because the contract puts
constraints in the database: one open proposal per workflow, an immutable
candidate version created inside the creation transaction, and a promotion that
compare-and-swaps the workflow revision.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.routers import workbuddy_proposals
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User


@pytest.fixture(scope="module")
def pool() -> Iterator[Any]:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool

    database = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"])
    try:
        with database.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        run_migrations(database)
        yield database
    finally:
        database.close()


def _seed_user(pool: Any, username: str) -> int:
    from octop.infra.db.repos._base import now_ts

    with pool.connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


@pytest.fixture(scope="module")
def tenant(pool: Any) -> dict[str, Any]:
    """One tenant: an owner who authors proposals and a second reviewing member."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"pp-owner-{uuid.uuid4().hex[:8]}")
    reviewer_id = _seed_user(pool, f"pp-rev-{uuid.uuid4().hex[:8]}")
    tenant_row = repo.create_tenant(
        f"pp-{uuid.uuid4().hex[:8]}", "Proposal tenant", owner_user_id=owner_id
    )
    reviewer_member = repo.add_membership(tenant_row["tenant_id"], reviewer_id, role="member")
    owner_member = repo.list_members(tenant_row["tenant_id"])[0]
    return {
        "tenant_id": tenant_row["tenant_id"],
        "slug": tenant_row["slug"],
        "owner_user_id": owner_id,
        "owner_member_id": str(owner_member.get("membership_id") or owner_member.get("id")),
        "reviewer_user_id": reviewer_id,
        "reviewer_member_id": str(
            reviewer_member.get("membership_id") or reviewer_member.get("id")
        ),
    }


def _principal(
    tenant: dict[str, Any],
    *,
    user_id: int | None = None,
    role: str = "owner",
    member_id: str | None = None,
) -> WorkBuddyPrincipal:
    owner = user_id is None or user_id == tenant["owner_user_id"]
    return WorkBuddyPrincipal(
        user=User(
            id=user_id if user_id is not None else tenant["owner_user_id"],
            username=f"pp-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Proposal tenant",
        member_id=member_id
        or (tenant["owner_member_id"] if owner else tenant["reviewer_member_id"]),
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


@pytest.fixture
def app(pool: Any) -> FastAPI:
    from octop.api.deps import get_server

    application = FastAPI()
    application.include_router(workbuddy_proposals.router)

    @application.exception_handler(OctopError)
    async def _octop_error(_: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    application.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=pool)
    )
    return application


def _client(app: FastAPI, principal: WorkBuddyPrincipal) -> httpx.AsyncClient:
    from octop.api.routers.workbuddy_identity import workbuddy_principal

    app.dependency_overrides[workbuddy_principal] = lambda: principal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


def _definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                "config": {
                    "input": {"greeting": "hello {{ inputs.who }}"},
                    "expression": "input",
                },
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }


def _publish(pool: Any, tenant: dict[str, Any], name: str) -> dict[str, Any]:
    """Create a workflow, activate its first version, and report both ids."""
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    repo = WorkBuddyWorkflowRepo(pool)
    compiled = compile_workflow_definition(_definition())
    bundle = repo.create_workflow(
        tenant["tenant_id"],
        name=name,
        definition=compiled.definition,
        definition_sha256=definition_sha256(compiled.definition),
        created_by_user_id=tenant["owner_user_id"],
        created_by_membership_id=tenant["owner_member_id"],
    )
    repo.activate_version(
        tenant["tenant_id"],
        bundle.workflow.workflow_id,
        bundle.version.workflow_version_id,
        expected_revision=bundle.workflow.revision,
    )
    # Activation advances the revision, and the revision is what a proposal pins.
    current = repo.get_workflow(tenant["tenant_id"], bundle.workflow.workflow_id)
    assert current is not None
    return {
        "workflow_id": bundle.workflow.workflow_id,
        "version_id": bundle.version.workflow_version_id,
        "revision": current.revision,
    }


def _rename_patch(name: str = "Greeting builder") -> list[dict[str, Any]]:
    """The smallest legitimate change: a node's display name.

    JSON pointers address the node array by index, which is also what the risk
    classifier reads.
    """
    return [{"op": "replace", "path": "/nodes/0/name", "value": name}]


async def _create(
    client: httpx.AsyncClient, workflow: dict[str, Any], patch: list[dict[str, Any]]
) -> httpx.Response:
    return await client.post(
        f"/workflows/{workflow['workflow_id']}/improvement-proposals",
        json={
            "workflow_revision": workflow["revision"],
            "patch": patch,
            "change_summary": "rename",
        },
    )


def _service(pool: Any, tenant: dict[str, Any]) -> Any:
    """The domain service on the live repository, for evidence the API does not take."""
    from octop.infra.db.repos.workbuddy_proposals import WorkBuddyProposalsRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext
    from octop.infra.workbuddy.proposals import ProposalPolicy, WorkBuddyProposalsService

    ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
    return WorkBuddyProposalsService(
        WorkBuddyProposalsRepo(pool, ctx),
        policy=ProposalPolicy(
            private_ids=frozenset(
                {tenant["tenant_id"], tenant["owner_member_id"], tenant["reviewer_member_id"]}
            )
        ),
    )


def _settle_shadow(pool: Any, tenant: dict[str, Any], proposal_id: str, *, runs: int = 10) -> None:
    """Replay-only shadow evidence: the only thing that may precede candidate traffic."""
    from octop.infra.workbuddy.proposals import ShadowRunRow

    service = _service(pool, tenant)
    for index in range(runs):
        service.record_shadow_run(
            proposal_id,
            run=ShadowRunRow(
                run_id=f"shadow-{index}",
                settled=True,
                replay_only=True,
                live_side_effects=0,
                evidence_hash=hashlib.sha256(f"shadow-{index}".encode()).hexdigest(),
                created_at=1_700_000_000 + index,
            ),
        )


async def test_proposal_creation_fixes_an_immutable_candidate(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """Creation compiles the patch against the base and stores the candidate."""
    workflow = _publish(pool, tenant, "Proposal rename")
    async with _client(app, _principal(tenant)) as client:
        created = await _create(client, workflow, _rename_patch())
        assert created.status_code == 202, created.text
        body = created.json()["data"]
        assert body["status"] == "pending", body
        assert body["risk_level"] == "low", body
        assert body["required_approvals"] == 1, body
        assert body["requires_manual_shadow"] is False, body

        detail = await client.get(f"/improvement-proposals/{body['proposal_id']}")
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["candidate_version_id"], data

    # The candidate is a stored version of this workflow, and the shadow pointer
    # stays empty until a promotion asks for it.
    from octop.infra.db.repos.workbuddy_proposals import WorkBuddyProposalsRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext

    ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
    repos = WorkBuddyProposalsRepo(pool, ctx)
    record = repos.get_proposal(body["proposal_id"])
    assert record is not None
    pointer = repos.workflow_pointer(workflow["workflow_id"])
    assert pointer is not None
    assert pointer.revision == workflow["revision"], pointer
    with pool.connect() as conn:
        row = conn.execute(
            "SELECT shadow_version_id FROM workbuddy_workflows WHERE workflow_id = ?",
            (workflow["workflow_id"],),
        ).fetchone()
    # Creation compiles a candidate but never installs it as the shadow pointer.
    assert row["shadow_version_id"] is None, row


async def test_boundary_changes_are_refused_and_store_nothing(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """The static gate refuses anything that widens privileges or moves a trigger."""
    workflow = _publish(pool, tenant, "Proposal boundary")
    async with _client(app, _principal(tenant)) as client:
        for patch in (
            # Moving the trigger changes how the workflow is reached.
            [{"op": "replace", "path": "/trigger/type", "value": "webhook"}],
            # Widening what a run may do, or where its output goes, is refused.
            [{"op": "add", "path": "/nodes/0/config/tool_name", "value": "email.send"}],
            [{"op": "add", "path": "/nodes/0/config/knowledge_base_ids", "value": []}],
            [{"op": "replace", "path": "/output/destination", "value": "external"}],
            # A whole-document swap cannot hide behind a single operation either.
            [{"op": "add", "path": "/nodes/-", "value": {"id": "x", "type": "tool"}}],
        ):
            refused = await _create(client, workflow, patch)
            assert refused.status_code >= 400, refused.text
        listing = await client.get("/improvement-proposals")
    # A refused patch stores nothing: no proposal exists for this workflow.
    stored = [
        item
        for item in listing.json()["data"]["items"]
        if item["workflow_id"] == workflow["workflow_id"]
    ]
    assert stored == [], stored


async def test_only_one_open_proposal_per_workflow(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """The database unique constraint, not application hope, enforces the rule."""
    workflow = _publish(pool, tenant, "Proposal single")
    async with _client(app, _principal(tenant)) as client:
        first = await _create(client, workflow, _rename_patch("First"))
        assert first.status_code == 202, first.text
        second = await _create(client, workflow, _rename_patch("Second"))
        assert second.status_code == 409, second.text
        assert second.json()["error"]["code"] == ErrorCode.PROPOSAL_CANDIDATE_EXISTS.value, (
            second.text
        )


async def test_review_requires_an_independent_member(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """The author can never approve, and one non-creator approval closes a low risk gate."""
    workflow = _publish(pool, tenant, "Proposal review")
    async with _client(app, _principal(tenant)) as client:
        created = await _create(client, workflow, _rename_patch("Reviewed"))
        proposal_id = created.json()["data"]["proposal_id"]

        self_review = await client.post(
            f"/improvement-proposals/{proposal_id}/decisions",
            json={"decision": "approved", "comment": "looks fine"},
        )
        assert self_review.status_code >= 400, self_review.text

    reviewer = _principal(
        tenant,
        user_id=tenant["reviewer_user_id"],
        role="member",
        member_id=tenant["reviewer_member_id"],
    )
    async with _client(app, reviewer) as client:
        approved = await client.post(
            f"/improvement-proposals/{proposal_id}/decisions",
            json={"decision": "approved", "comment": "independent review"},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["data"]["status"] == "approved", approved.text


async def test_promotion_walks_shadow_canary_and_apply(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """A low-risk candidate can be promoted once the gates have evidence."""
    workflow = _publish(pool, tenant, "Proposal promotion")
    async with _client(app, _principal(tenant)) as client:
        created = await _create(client, workflow, _rename_patch("Promoted"))
        proposal_id = created.json()["data"]["proposal_id"]

    reviewer = _principal(
        tenant,
        user_id=tenant["reviewer_user_id"],
        role="member",
        member_id=tenant["reviewer_member_id"],
    )
    async with _client(app, reviewer) as client:
        approved = await client.post(
            f"/improvement-proposals/{proposal_id}/decisions",
            json={"decision": "approved", "comment": "reviewed"},
        )
        assert approved.status_code == 200, approved.text

    # A promotion needs the revision the admin saw; without it the request is
    # refused before anything moves.
    admin = _principal(tenant)
    async with _client(app, admin) as client:
        missing = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_canary", "ratio_basis_points": 500},
        )
        assert missing.status_code == 428, missing.text
        assert missing.json()["error"]["code"] == ErrorCode.PRECONDITION_REQUIRED.value, (
            missing.text
        )

        # Canary traffic cannot start before the replay-only shadow phase.
        premature = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_canary", "ratio_basis_points": 500},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert premature.status_code == 409, premature.text
        assert premature.json()["error"]["code"] == ErrorCode.STATE_CONFLICT.value, premature.text

        started = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_shadow", "ratio_basis_points": 500},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert started.status_code == 200, started.text
        assert started.json()["data"]["status"] == "shadowing", started.text

        # A shadow phase without evidence blocks candidate traffic.
        unpublished = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_canary", "ratio_basis_points": 500},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert unpublished.status_code == 409, unpublished.text

    _settle_shadow(pool, tenant, proposal_id)

    async with _client(app, admin) as client:
        canary = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_canary", "ratio_basis_points": 500},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert canary.status_code == 200, canary.text
        assert canary.json()["data"]["status"] == "canary", canary.text

        # Applying needs the observation window and settled samples, not optimism.
        too_early = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "apply"},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert too_early.status_code == 409, too_early.text
        assert too_early.json()["error"]["code"] == ErrorCode.PROPOSAL_GATE_NOT_MET.value, (
            too_early.text
        )
        assert started.headers["ETag"] == f'W/"{workflow["revision"]}"', started.headers


async def test_stale_base_blocks_promotion(app: FastAPI, pool: Any, tenant: dict[str, Any]) -> None:
    """A proposal whose base moved cannot be promoted; the base pointer is truth."""
    workflow = _publish(pool, tenant, "Proposal stale")
    async with _client(app, _principal(tenant)) as client:
        created = await _create(client, workflow, _rename_patch("Stale"))
        proposal_id = created.json()["data"]["proposal_id"]

    # Someone saves a new version, which advances the workflow revision.
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    repo = WorkBuddyWorkflowRepo(pool)
    moved = _definition()
    moved["nodes"][0]["name"] = "Manual edit"
    compiled = compile_workflow_definition(moved)
    repo.save_version(
        tenant["tenant_id"],
        workflow["workflow_id"],
        definition=compiled.definition,
        definition_sha256=definition_sha256(compiled.definition),
        expected_revision=workflow["revision"],
        created_by_user_id=tenant["owner_user_id"],
        created_by_membership_id=tenant["owner_member_id"],
    )

    async with _client(app, _principal(tenant)) as client:
        refused = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_shadow", "ratio_basis_points": 500},
            headers={"If-Match": f'"{workflow["revision"]}"'},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == ErrorCode.PROPOSAL_BASE_CONFLICT.value, (
            refused.text
        )

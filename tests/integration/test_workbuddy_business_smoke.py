"""One business scenario across the whole WorkBuddy spine.

Every other integration file proves one slice. This one drives the modules
together on a live PostgreSQL schema, because the defects that survive
per-slice tests are the ones that live in the seams: an approval that never
reaches its write, a reconciliation that does not settle the branch, a canary
that routes to a version the execution cannot run, a proposal whose shadow
phase cannot find the recording it needs to replay.

The scenario follows the contract's own storyline: publish a workflow that
authorizes an external bid submission, approve it, lose the provider's answer,
reconcile the evidence, then improve the workflow through a shadowed canary.
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

from octop.api.deps import get_server
from octop.api.routers import (
    workbuddy_catalog,
    workbuddy_identity,
    workbuddy_marketplace,
    workbuddy_proposals,
    workbuddy_runtime,
    workbuddy_workflows,
)
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPlatformPrincipal,
    WorkBuddyPrincipal,
    workbuddy_principal,
)
from octop.infra.errors import OctopError
from octop.infra.users.identity import Role, User
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]


@pytest.fixture(scope="module")
def pool() -> Iterator[Any]:
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import PostgresPool

    database = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"])
    try:
        with database.connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
            # The upstream migrations declare a pgvector column, so the schema
            # reset above has to be followed by the deployment prerequisite.
            available = conn.execute(
                "SELECT 1 AS present FROM pg_available_extensions WHERE name = 'vector'"
            ).fetchone()
            if available is None:
                pytest.skip("pgvector is required by the upstream migrations")
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
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
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"sm-owner-{uuid.uuid4().hex[:8]}")
    reviewer_id = _seed_user(pool, f"sm-rev-{uuid.uuid4().hex[:8]}")
    tenant_row = repo.create_tenant(
        f"sm-{uuid.uuid4().hex[:8]}", "Smoke tenant", owner_user_id=owner_id
    )
    reviewer = repo.add_membership(tenant_row["tenant_id"], reviewer_id, role="member")
    members = repo.list_members(tenant_row["tenant_id"])
    owner = next(member for member in members if str(member.get("role")) == "owner")

    def _id(member: Any) -> str:
        return str(member.get("membership_id") or member.get("id"))

    return {
        "tenant_id": tenant_row["tenant_id"],
        "slug": tenant_row["slug"],
        "owner_user_id": owner_id,
        "owner_member_id": _id(owner),
        "reviewer_user_id": reviewer_id,
        "reviewer_member_id": _id(reviewer),
    }


def _principal(
    tenant: dict[str, Any],
    *,
    role: str = "owner",
    user_id: int | None = None,
    member_id: str | None = None,
) -> WorkBuddyPrincipal:
    owner = user_id is None or user_id == tenant["owner_user_id"]
    return WorkBuddyPrincipal(
        user=User(
            id=user_id if user_id is not None else tenant["owner_user_id"],
            username=f"sm-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Smoke tenant",
        member_id=member_id
        or (tenant["owner_member_id"] if owner else tenant["reviewer_member_id"]),
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


class _LostResponsePort:
    """The provider never answers: the write's outcome is unknown."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute_tool(self, *, node: Any, activation: Any, idempotency_key: str) -> Any:
        from octop.infra.workbuddy.runtime import UnresolvedToolOutcome

        self.calls.append(node.id)
        raise UnresolvedToolOutcome(
            operation_key=idempotency_key,
            external_request_id="provider-smoke-1",
            dispatched_at="2026-09-19T00:00:00Z",
            tool_revision="rev-1",
            parameters_digest=hashlib.sha256(b"params").hexdigest(),
            detail="the provider never answered",
        )

    def execute_llm(self, *, node: Any, activation: Any) -> Any:  # pragma: no cover
        raise AssertionError("this workflow has no llm node")

    def respond_chat(
        self, *, session_id: str, message: str, history: Any
    ) -> Any:  # pragma: no cover
        raise AssertionError("chat is not part of this workflow")


@pytest.fixture
def app(pool: Any, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Every WorkBuddy router on one application, over one live schema."""
    from octop.infra.workbuddy.runtime import (
        ProposalCanaryDirectory,
        WorkBuddyRuntimeService,
    )

    port = _LostResponsePort()
    # The same wiring the router uses, so canary routing is exercised too.
    service = WorkBuddyRuntimeService(pool, effects=port, canary=ProposalCanaryDirectory(pool))
    monkeypatch.setattr(workbuddy_runtime, "_service", lambda server: service)

    application = FastAPI()
    for router in (
        workbuddy_identity.router,
        workbuddy_catalog.router,
        workbuddy_workflows.router,
        workbuddy_runtime.router,
        workbuddy_proposals.router,
        workbuddy_marketplace.router,
    ):
        application.include_router(router)

    @application.exception_handler(OctopError)
    async def _octop_error(_: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content=exc.to_envelope(),
            headers=dict(exc.headers or {}),
        )

    application.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=pool, rate_limit_store=None)
    )
    application.state.smoke_port = port
    return application


def _client(app: FastAPI, principal: WorkBuddyPrincipal) -> httpx.AsyncClient:
    app.dependency_overrides[workbuddy_principal] = lambda: principal
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    )


def _definitions() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"bid": {"type": "string", "required": True, "default": "BID-1"}},
        "nodes": [
            {
                "id": "prepare",
                "type": "transform",
                "name": "Prepare the bid",
                "config": {
                    "input": {"bid": "{{ inputs.bid }}"},
                    "expression": "input",
                },
                "save_as": "prepared",
            },
            {
                "id": "authorize",
                "type": "approval",
                "name": "Authorize the submission",
                "config": {
                    "approval_message": "Submit {{ nodes.prepare.output }}?",
                    "target_node_id": "submit",
                },
            },
            {
                "id": "submit",
                "type": "tool",
                "name": "Submit the bid",
                "config": {
                    "tool_name": "bidding.submit",
                    "parameters": {"bid": "{{ nodes.prepare.output }}"},
                },
                "save_as": "submission",
            },
            {
                "id": "report",
                "type": "transform",
                "name": "Report the outcome",
                "config": {"input": "submitted", "expression": "input"},
                "save_as": "outcome",
            },
        ],
        "edges": [
            {"from": "prepare", "to": "authorize"},
            {"from": "submit", "to": "report"},
        ],
    }


async def test_the_spine_carries_one_business_scenario(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """Publish, authorize, lose the write, reconcile it, then improve the flow."""
    from octop.infra.db.repos.workbuddy_proposals import WorkBuddyProposalsRepo
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext
    from octop.infra.workbuddy.proposals import ProposalPolicy, WorkBuddyProposalsService
    from octop.infra.workbuddy.runtime import RuntimeShadowRunner
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    owner = _principal(tenant)
    reviewer = _principal(
        tenant,
        user_id=tenant["reviewer_user_id"],
        role="member",
        member_id=tenant["reviewer_member_id"],
    )

    # 0. The catalog approves the tool the workflow submits through: without the
    #    grant a candidate that uses it cannot be evaluated at all.
    from octop.infra.db.repos.workbuddy_catalog import WorkBuddyCatalogRepo

    catalog = WorkBuddyCatalogRepo(pool)
    tool_revision = catalog.publish_tool(
        adapter_key="bidding",
        tool_key="bidding.submit",
        display_name="Submit a bid",
        actor_user_id=tenant["owner_user_id"],
    )
    catalog.update_capabilities(
        tenant["tenant_id"],
        actor_member_id=tenant["owner_member_id"],
        tool_revision_ids=[tool_revision.tool_revision_id],
    )

    # 1. Publish the workflow with the approval candidates filled in.
    definition = _definitions()
    definition["nodes"][1]["config"]["approver_user_ids"] = [tenant["owner_member_id"]]
    compiled = compile_workflow_definition(definition)
    workflows = WorkBuddyWorkflowRepo(pool)
    bundle = workflows.create_workflow(
        tenant["tenant_id"],
        name="Bid submission smoke",
        definition=compiled.definition,
        definition_sha256=definition_sha256(compiled.definition),
        created_by_user_id=tenant["owner_user_id"],
        created_by_membership_id=tenant["owner_member_id"],
    )
    workflows.activate_version(
        tenant["tenant_id"],
        bundle.workflow.workflow_id,
        bundle.version.workflow_version_id,
        expected_revision=bundle.workflow.revision,
    )
    workflow_id = bundle.workflow.workflow_id
    current = workflows.get_workflow(tenant["tenant_id"], workflow_id)
    assert current is not None
    revision = current.revision

    async with _client(app, owner) as client:
        # 2. A run parks at the authorization.
        accepted = await client.post(
            f"/workflows/{workflow_id}/execute", json={"inputs": {"bid": "BID-42"}}
        )
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]
        parked = (await client.get(f"/executions/{execution_id}")).json()["data"]
        assert parked["status"] == "waiting_approval", parked
        assert parked["wait_reasons"] == ["approval"], parked
        assert parked["waiting_steps"] == ["authorize"], parked

        # 3. The owner authorizes it, which dispatches the write.
        listed = await client.get("/approval-requests")
        request_item = next(
            item
            for item in listed.json()["data"]["items"]
            if item["status"] == "pending" and item["execution_id"] == execution_id
        )
        challenge = await client.post(f"/approval-requests/{request_item['id']}/challenge")
        approved = await client.post(
            f"/executions/{execution_id}/resume",
            json={
                "approval_request_id": request_item["id"],
                "decision": "approved",
                "token": challenge.json()["token"],
            },
        )
        assert approved.status_code == 200, approved.text

        # 4. The provider never answered, so the execution waits for evidence.
        waiting = (await client.get(f"/executions/{execution_id}")).json()["data"]
        assert waiting["status"] == "waiting_reconciliation", waiting
        assert waiting["wait_reasons"] == ["reconciliation"], waiting

        # 5. An operator reconciles the write with the provider's record.
        ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
        evidence_ref = WorkBuddyProposalsRepo and None  # placeholder, replaced below
        from octop.infra.db.repos.workbuddy_runtime import WorkBuddyRuntimeRepo

        runtime_repo = WorkBuddyRuntimeRepo(pool)
        evidence = {"query": "provider lookup", "result": {"bid_id": "BID-42"}}
        evidence_ref = runtime_repo.insert_payload(
            ctx,
            tenant_id=tenant["tenant_id"],
            execution_id=execution_id,
            kind="reconciliation_evidence",
            node_id="submit",
            content=evidence,
            sha256=hashlib.sha256(b"evidence").hexdigest(),
            size_bytes=len(str(evidence)),
        )
        reconciled = await client.post(
            f"/executions/{execution_id}/reconciliations",
            json={
                "step_id": "submit",
                "decision": "confirmed_success",
                "evidence_ref": evidence_ref,
                "external_reference": "provider-smoke-1",
                "reason": "the provider confirms the bid was received",
            },
        )
        assert reconciled.status_code == 200, reconciled.text
        settled = (await client.get(f"/executions/{execution_id}")).json()["data"]
        assert settled["status"] == "success", settled
        assert settled["outputs"]["submission"] == {"bid_id": "BID-42"}, settled

    # 6. The workflow owner proposes an improvement on top of the settled run.
    async with _client(app, owner) as client:
        created = await client.post(
            f"/workflows/{workflow_id}/improvement-proposals",
            json={
                "workflow_revision": revision,
                "patch": [{"op": "replace", "path": "/nodes/3/name", "value": "Report clearly"}],
                "change_summary": "clearer report",
            },
        )
        assert created.status_code == 202, created.text
        proposal_id = created.json()["data"]["proposal_id"]
        assert created.json()["data"]["risk_level"] == "low", created.text

    async with _client(app, reviewer) as client:
        decided = await client.post(
            f"/improvement-proposals/{proposal_id}/decisions",
            json={"decision": "approved", "comment": "independent review"},
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["data"]["status"] == "approved", decided.text

    # 7. The shadow phase replays the settled run's recordings.
    async with _client(app, owner) as client:
        started = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_shadow", "ratio_basis_points": 5000},
            headers={"If-Match": f'"{revision}"'},
        )
        assert started.status_code == 200, started.text
        assert started.json()["data"]["status"] == "shadowing", started.text

    runner = RuntimeShadowRunner(pool, tenant["tenant_id"], runs=10)
    produced = runner.produce(proposal_id)
    assert all(run.replay_only and run.live_side_effects == 0 for run in produced), produced

    ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
    proposals_service = WorkBuddyProposalsService(
        WorkBuddyProposalsRepo(pool, ctx), policy=ProposalPolicy()
    )
    for run in produced:
        proposals_service.record_shadow_run(proposal_id, run=run)

    async with _client(app, owner) as client:
        canary = await client.post(
            f"/improvement-proposals/{proposal_id}/promote",
            json={"action": "start_canary", "ratio_basis_points": 5000},
            headers={"If-Match": f'"{revision}"'},
        )
        assert canary.status_code == 200, canary.text
        assert canary.json()["data"]["status"] == "canary", canary.text
        candidate_version_id = canary.json()["data"]["candidate_version_id"]

        # 8. Real traffic splits by the published bucket, in one execution each.
        routed: dict[str, str] = {}
        for _ in range(60):
            member_id = str(uuid.uuid4())
            subject = f"user:{member_id}"
            bucket = (
                int.from_bytes(
                    hashlib.sha256(
                        f"{tenant['tenant_id']}:{workflow_id}:{subject}".encode()
                    ).digest()[:8],
                    "big",
                )
                % 10000
            )
            expected = "canary" if bucket < 5000 else "baseline"
            if expected in routed:
                continue
            principal = _principal(tenant, member_id=member_id)
            async with _client(app, principal) as per_subject:
                ran = await per_subject.post(
                    f"/workflows/{workflow_id}/execute", json={"inputs": {"bid": "BID-7"}}
                )
                assert ran.status_code == 202, ran.text
                execution = ran.json()["data"]
            assert execution["cohort"] == expected, execution
            assert execution["proposal_id"] == proposal_id, execution
            expected_version = (
                candidate_version_id if expected == "canary" else bundle.version.workflow_version_id
            )
            assert execution["workflow_version_id"] == expected_version, execution
            routed[expected] = execution["id"]
            if len(routed) == 2:
                break

    assert set(routed) == {"canary", "baseline"}, routed

    # 9. The proposal carries the replay proof the gate judged, and the routed
    #    runs are parked at their own authorization rather than having written.
    async with _client(app, owner) as client:
        detail = (await client.get(f"/improvement-proposals/{proposal_id}")).json()["data"]
        assert detail["status"] == "canary", detail
        assert detail["candidate_version_id"] == candidate_version_id, detail
        assert detail["shadow_proof"]["complete"] is True, detail
        assert detail["shadow_proof"]["settled_runs"] >= 10, detail
        for execution_id in routed.values():
            routed_run = (await client.get(f"/executions/{execution_id}")).json()["data"]
            assert routed_run["status"] == "waiting_approval", routed_run
    # The provider was asked exactly once: only the authorized run dispatched.
    assert app.state.smoke_port.calls == ["submit"], app.state.smoke_port.calls


# ── the marketplace is only useful if what it installs actually runs ─────────
#
# The marketplace slice proves a submission is sanitized, reviewed and published,
# and that an install lands a workflow version. What no other file proves is the
# join: the version an install creates is one this spine can activate and execute.
# The template below is deliberately transform-only, so the run needs no provider.

PLATFORM_DEP = workbuddy_marketplace._Platform.__metadata__[0].dependency  # type: ignore[attr-defined]

_TEMPLATE_DEFINITION: dict[str, Any] = {
    "schema_version": 1,
    "trigger": {"type": "manual", "config": {}},
    "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
    "nodes": [
        {
            "id": "hello",
            "type": "transform",
            "name": "Build greeting",
            "config": {"input": {"greeting": "hello {{ inputs.who }}"}, "expression": "inputs"},
            "save_as": "greeting",
        }
    ],
    "edges": [],
}


def _submission_body(definition: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"Smoke template {uuid.uuid4().hex[:6]}",
        "summary": "Greets the caller.",
        "industry": "general",
        "definition": definition,
        "license_id": "octop-community",
        "license_text": "Publish terms: sanitized templates only.",
        "capabilities": [],
    }


async def _publish_template(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> dict[str, str]:
    """Submit, freeze and platform-approve one template version."""
    async with _client(app, _principal(tenant)) as client:
        created = await client.post(
            "/marketplace/submissions", json=_submission_body(_TEMPLATE_DEFINITION)
        )
        assert created.status_code == 201, created.text
        submission = created.json()["data"]
        frozen = await client.post(
            f"/marketplace/submissions/{submission['id']}/submit",
            json={"expected_revision": submission["revision"]},
        )
        assert frozen.status_code == 200, frozen.text
        revision = frozen.json()["data"]["revision"]

    reviewer_id = _seed_user(pool, f"sm-platform-{uuid.uuid4().hex[:8]}")
    app.dependency_overrides[PLATFORM_DEP] = lambda: WorkBuddyPlatformPrincipal(
        user=User(id=reviewer_id, username="sm-platform", role=Role.USER, display_name=None),
        audience="workbuddy-platform",
        claims={},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workbuddy.test"
    ) as platform:
        decided = await platform.post(
            f"/platform/submissions/{tenant['tenant_id']}/{submission['id']}/decisions",
            json={
                "decision": "approved",
                "platform_review_ref": "smoke-review-1",
                "expected_revision": revision,
                "publication": {"version": "1.0.0", "publisher_display": "Octop Labs"},
            },
        )
        assert decided.status_code == 200, decided.text
        published = decided.json()["data"]["published_version"]
    return {
        "template_id": published["template_id"],
        "version_id": published["template_version_id"],
    }


async def test_installed_template_version_runs_on_the_spine(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T24 × the spine: activate what the install created and run it to completion."""
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.workbuddy import marketplace as marketplace_service

    published = await _publish_template(app, pool, tenant)

    async with _client(app, _principal(tenant)) as client:
        version = (
            await client.get(
                f"/marketplace/templates/{published['template_id']}"
                f"/versions/{published['version_id']}"
            )
        ).json()["data"]
        installed = await client.post(
            f"/marketplace/templates/{published['template_id']}/install",
            json={
                "template_version_id": published["version_id"],
                "consent": {
                    "accepted": True,
                    "template_version_id": published["version_id"],
                    "license_text_hash": version["license_text_hash"],
                    # The consent hashes are recomputed the way the platform does:
                    # canonical JSON over the capabilities the version declares.
                    "capabilities_hash": marketplace_service.sha256_hex(
                        marketplace_service.canonical_json(
                            list(version["required_capabilities"])
                        )
                    ),
                },
                "bindings": {},
                "credential_bindings": {},
                "workflow_name": f"Installed greeting {uuid.uuid4().hex[:6]}",
            },
        )
        assert installed.status_code == 202, installed.text
        installation_id = installed.json()["data"]["installation"]["id"]
        state = (
            await client.get(f"/marketplace/installations/{installation_id}")
        ).json()["data"]
        assert state["status"] == "installed", state
        workflow_id = state["workflow_id"]
        workflow_version_id = state["installed_version_id"]

        # The install created the version but did not activate it: the tenant
        # decides when an installed workflow starts serving traffic.
        repo = WorkBuddyWorkflowRepo(pool)
        workflow = repo.get_workflow(tenant["tenant_id"], workflow_id)
        assert workflow is not None
        repo.activate_version(
            tenant["tenant_id"],
            workflow_id,
            workflow_version_id,
            expected_revision=workflow.revision,
        )

        accepted = await client.post(
            f"/workflows/{workflow_id}/execute", json={"inputs": {"who": "smoke"}}
        )
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]
        execution = (await client.get(f"/executions/{execution_id}")).json()["data"]
        assert execution["status"] == "success", execution
        assert execution["workflow_version_id"] == workflow_version_id, execution

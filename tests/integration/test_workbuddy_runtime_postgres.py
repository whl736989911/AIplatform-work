"""Live-PostgreSQL acceptance gate for the WorkBuddy runtime slice (018).

Drives the real runtime router and service against a real database, because the
guarantees under test live in the execution facts: the DAG aggregation that turns
node outcomes into an execution status, idempotent acceptance of an execution,
one-time approval consumption, and the tenant suspension rule that blocks new
starts without disturbing work already in flight.

Enable with::

    export OCTOP_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:15441/octop_test'

Without it the module is skipped; SQLite cannot stand in for any of this.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.routers import workbuddy_runtime
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal
from octop.infra.db.repos._base import now_ts
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]


def hello_definition() -> dict[str, Any]:
    """One transform node: the smallest end-to-end execution."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "hello",
                "type": "transform",
                "name": "Build greeting",
                # ``input`` is the rendered binding, ``inputs`` the raw workflow
                # inputs, so the CEL expression selects the rendered object.
                "config": {
                    "input": {"greeting": "hello {{ inputs.who }}"},
                    "expression": "input",
                },
                "save_as": "greeting",
            }
        ],
        "edges": [],
    }


def conditional_definition() -> dict[str, Any]:
    """A condition with two branches and a join, to exercise edge resolution."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"approved": {"type": "boolean", "required": True}},
        "nodes": [
            {
                "id": "choose",
                "type": "condition",
                "name": "Choose branch",
                "config": {"expression": "inputs.approved"},
            },
            {
                "id": "yes",
                "type": "transform",
                "name": "Accepted",
                "config": {"input": "accepted", "expression": "input"},
                "save_as": "accepted",
            },
            {
                "id": "no",
                "type": "transform",
                "name": "Rejected",
                "config": {"input": "rejected", "expression": "input"},
                "save_as": "rejected",
            },
            {
                "id": "join",
                "type": "transform",
                "name": "Join",
                "config": {
                    "input": None,
                    "expression": (
                        "has(outputs.accepted) ? outputs.accepted"
                        " : (has(outputs.rejected) ? outputs.rejected : 'none')"
                    ),
                },
                "save_as": "out",
            },
        ],
        "edges": [
            {"from": "choose", "to": "yes", "when": "true"},
            {"from": "choose", "to": "no", "when": "false"},
            {"from": "yes", "to": "join"},
            {"from": "no", "to": "join"},
        ],
    }


def approval_definition(approver_membership_ids: list[str]) -> dict[str, Any]:
    """A report review: transform, then an approval node bound to candidates."""
    definition = hello_definition()
    definition["nodes"] = [
        *definition["nodes"],
        {
            "id": "review",
            "type": "approval",
            "name": "Review greeting",
            "config": {
                "approval_message": "Review {{ nodes.hello.output }}",
                "approver_user_ids": approver_membership_ids,
                "timeout_hours": 24,
            },
            "save_as": "reviewed",
        },
    ]
    definition["edges"] = [{"from": "hello", "to": "review"}]
    return definition


def _seed_user(pool: Any, username: str) -> int:
    with pool.connect() as conn:
        row = conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, 'hash', 'user', ?) RETURNING id",
            (username, now_ts()),
        ).fetchone()
    return int(row["id"])


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


@pytest.fixture(scope="module")
def tenant(pool: Any) -> dict[str, Any]:
    """One tenant with an owner and a second member (both approvable)."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    repo = WorkBuddyIdentityRepo(pool)
    owner_id = _seed_user(pool, f"rt-owner-{uuid.uuid4().hex[:8]}")
    reviewer_id = _seed_user(pool, f"rt-rev-{uuid.uuid4().hex[:8]}")
    tenant_row = repo.create_tenant(
        f"rt-{uuid.uuid4().hex[:8]}", "Runtime tenant", owner_user_id=owner_id
    )
    reviewer_member = repo.add_membership(tenant_row["tenant_id"], reviewer_id, role="member")
    members = repo.list_members(tenant_row["tenant_id"])
    owner_member = next(member for member in members if str(member.get("role")) == "owner")
    return {
        "tenant_id": tenant_row["tenant_id"],
        "slug": tenant_row["slug"],
        "owner_user_id": owner_id,
        "owner_member_id": owner_membership_id(owner_member),
        "reviewer_user_id": reviewer_id,
        "reviewer_member_id": owner_membership_id(reviewer_member),
    }


def owner_membership_id(member: dict[str, Any]) -> str:
    return str(member.get("membership_id") or member.get("id"))


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
            username=f"rt-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Runtime tenant",
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
    application.include_router(workbuddy_runtime.router)

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


def _publish(pool: Any, tenant: dict[str, Any], definition: dict[str, Any], name: str) -> str:
    """Create, activate, and return a workflow id (creates a new one each call)."""
    from octop.infra.db.repos.workbuddy_workflows import WorkBuddyWorkflowRepo
    from octop.infra.workbuddy.workflow_compiler import (
        compile_workflow_definition,
        definition_sha256,
    )

    repo = WorkBuddyWorkflowRepo(pool)
    compiled = compile_workflow_definition(definition)
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
    return bundle.workflow.workflow_id


async def test_hello_execution_runs_to_completion(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T06: execute, then read the terminal execution with its output."""
    workflow_id = _publish(pool, tenant, hello_definition(), "Hello runtime")
    principal = _principal(tenant)
    async with _client(app, principal) as client:
        accepted = await client.post(
            f"/workflows/{workflow_id}/execute", json={"inputs": {"who": "runtime"}}
        )
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]

        fetched = await client.get(f"/executions/{execution_id}")
        assert fetched.status_code == 200, fetched.text
        data = fetched.json()["data"]

    assert data["status"] == "success", data
    assert data["outputs"] == {"greeting": {"greeting": "hello runtime"}}, data
    assert data["workflow_version_id"]


async def test_condition_branches_and_joins_once(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T07/T08: exactly one branch runs, the join executes once with its value."""
    workflow_id = _publish(pool, tenant, conditional_definition(), "Condition runtime")
    principal = _principal(tenant)
    async with _client(app, principal) as client:
        for approved, expected in ((True, "accepted"), (False, "rejected")):
            accepted = await client.post(
                f"/workflows/{workflow_id}/execute", json={"inputs": {"approved": approved}}
            )
            assert accepted.status_code == 202, accepted.text
            execution_id = accepted.json()["data"]["id"]
            fetched = await client.get(f"/executions/{execution_id}")
            assert fetched.status_code == 200, fetched.text
            data = fetched.json()["data"]
            assert data["status"] == "success", data
            steps = {step["node_id"]: step for step in data["steps"]}
            assert data["outputs"]["out"] == expected, data
            # Exactly one branch ran; the other never executed.
            taken, untaken = ("yes", "no") if expected == "accepted" else ("no", "yes")
            assert steps[taken]["status"] == "success", steps
            assert steps[untaken]["status"] == "skipped", steps
            # A branch nobody selected was not chosen, not blocked.
            assert steps[untaken]["skip_reason"] == "not_selected", steps
            # The join merges both branches and must run once, not twice.
            assert [s for s in data["steps"] if s["node_id"] == "join"].__len__() == 1, steps

        listing = await client.get("/executions")
        assert listing.status_code == 200
        assert len(listing.json()["data"]["items"]) >= 2


async def test_idempotency_key_replays_and_conflicts(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T12: same key and inputs return the same execution; different inputs 409."""
    workflow_id = _publish(pool, tenant, hello_definition(), "Idempotent runtime")
    principal = _principal(tenant)
    key = f"key-{uuid.uuid4()}"
    async with _client(app, principal) as client:
        first = await client.post(
            f"/workflows/{workflow_id}/execute",
            headers={"Idempotency-Key": key},
            json={"inputs": {"who": "once"}},
        )
        assert first.status_code == 202, first.text
        second = await client.post(
            f"/workflows/{workflow_id}/execute",
            headers={"Idempotency-Key": key},
            json={"inputs": {"who": "once"}},
        )
        assert second.status_code == 202, second.text
        assert second.json()["data"]["id"] == first.json()["data"]["id"]

        conflict = await client.post(
            f"/workflows/{workflow_id}/execute",
            headers={"Idempotency-Key": key},
            json={"inputs": {"who": "twice"}},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["error"]["code"] == ErrorCode.IDEMPOTENCY_CONFLICT.value


async def test_terminal_execution_cannot_be_cancelled(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T13: cancel is refused once the execution reached a terminal state."""
    workflow_id = _publish(pool, tenant, hello_definition(), "Cancel runtime")
    principal = _principal(tenant)
    async with _client(app, principal) as client:
        accepted = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        execution_id = accepted.json()["data"]["id"]
        cancelled = await client.post(
            f"/executions/{execution_id}/cancel", json={"reason": "no longer needed"}
        )
        assert cancelled.status_code == 409, cancelled.text
        assert cancelled.json()["error"]["code"] == ErrorCode.STATE_CONFLICT.value


async def test_suspended_tenant_cannot_start_new_executions(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T18: suspension blocks new starts; the fact store is the authority."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    workflow_id = _publish(pool, tenant, hello_definition(), "Suspended runtime")
    repo = WorkBuddyIdentityRepo(pool)
    try:
        repo.suspend_tenant(tenant["tenant_id"], reason="billing review")
        suspended = _principal({**tenant})
        async with _client(app, suspended) as client:
            refused = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] in {
            ErrorCode.TENANT_SUSPENDED.value,
            ErrorCode.TENANT_SUSPENDED.value,
        }
    finally:
        repo.restore_tenant(tenant["tenant_id"], reason="test cleanup")


async def test_approval_round_trip_and_single_consumption(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T09-T11: a pending approval is consumed once and resumes the execution."""
    from octop.infra.workbuddy.runtime import MembershipApproverResolver

    definition = approval_definition([tenant["owner_member_id"]])
    workflow_id = _publish(pool, tenant, definition, "Approval runtime")
    principal = _principal(tenant)
    async with _client(app, principal) as client:
        accepted = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]

        waiting = await client.get(f"/executions/{execution_id}")
        assert waiting.json()["data"]["status"] == "waiting_approval", waiting.text

        pending = await client.get("/approval-requests")
        assert pending.status_code == 200, pending.text
        requests = [item for item in pending.json()["data"]["items"] if item["status"] == "pending"]
        assert requests, pending.text
        approval_id = requests[0]["id"]

        challenge = await client.post(f"/approval-requests/{approval_id}/challenge")
        assert challenge.status_code == 200, challenge.text
        token = challenge.json()["token"]

        approved = await client.post(
            f"/executions/{execution_id}/resume",
            json={"approval_request_id": approval_id, "decision": "approved", "token": token},
        )
        assert approved.status_code == 200, approved.text

        settled = await client.get(f"/executions/{execution_id}")
        assert settled.json()["data"]["status"] in {"success", "running"}, settled.text
        step = settled.json()["data"]["steps"][0]
        assert step["duration_ms"] is not None, step
        assert step["started_at"] and step["finished_at"], step

        replay = await client.post(
            f"/executions/{execution_id}/resume",
            json={"approval_request_id": approval_id, "decision": "approved", "token": token},
        )
        assert replay.status_code >= 400, replay.text

    assert MembershipApproverResolver is not None


async def test_zero_valid_approvers_fails_the_node(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T29: no eligible candidate means the node fails instead of waiting forever."""
    definition = approval_definition([str(uuid.uuid4())])
    # An independent branch must still run to completion while the approval
    # node fails on its own.
    definition["nodes"].append(
        {
            "id": "audit",
            "name": "Unrelated work",
            "type": "transform",
            "config": {"input": "audit", "expression": "input"},
            "save_as": "audit",
        }
    )
    definition["edges"].append({"from": "hello", "to": "audit"})
    workflow_id = _publish(pool, tenant, definition, "No approver runtime")
    principal = _principal(tenant)
    async with _client(app, principal) as client:
        accepted = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]
        fetched = await client.get(f"/executions/{execution_id}")
        data = fetched.json()["data"]
        pending = await client.get("/approval-requests")

    assert pending.status_code == 200, pending.text
    waiting = [item for item in pending.json()["data"]["items"] if item["status"] == "pending"]
    # Nobody can decide, so no request is left behind for the tenant to chase.
    assert not waiting, waiting
    assert data["status"] == "partial", data  # the independent branch succeeded
    assert data["error_code"] == ErrorCode.APPROVAL_NO_VALID_APPROVER.value, data
    steps = {step["node_id"]: step for step in data["steps"]}
    assert steps["review"]["status"] == "failed", steps
    assert steps["review"]["error_code"] == ErrorCode.APPROVAL_NO_VALID_APPROVER.value, steps
    assert steps["audit"]["status"] == "success", steps


# --------------------------------------------------------------------------- #
# unknown external writes (contract 5.1.6 / T13)
# --------------------------------------------------------------------------- #


class _LostResponsePort:
    """A trusted adapter whose external write loses its response.

    It records every dispatch so a test can prove the write is never sent twice.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def execute_tool(self, *, node: Any, activation: Any, idempotency_key: str) -> Any:
        from octop.infra.workbuddy.runtime import UnresolvedToolOutcome

        self.calls.append({"node_id": node.id, "key": idempotency_key})
        raise UnresolvedToolOutcome(
            operation_key=idempotency_key,
            external_request_id="provider-operation-20260918-001",
            dispatched_at="2026-09-18T10:00:00Z",
            tool_revision="rev-3",
            parameters_digest="d41d8cd98f00b204e9800998ecf8427e",
            detail="the provider never answered",
        )

    def execute_llm(self, *, node: Any, activation: Any) -> Any:  # pragma: no cover
        raise AssertionError("this workflow has no llm node")

    def respond_chat(
        self, *, session_id: str, message: str, history: Any
    ) -> Any:  # pragma: no cover
        raise AssertionError("chat is not part of this workflow")


def external_write_definition(approver_membership_ids: list[str]) -> dict[str, Any]:
    """Approve, then submit: the conservative shape for an external write."""
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
            },
            {
                "id": "review",
                "type": "approval",
                "name": "Authorize the submission",
                "config": {
                    "approval_message": "Authorize the bid submission?",
                    "approver_user_ids": approver_membership_ids,
                    "target_node_id": "submit",
                },
            },
            {
                "id": "submit",
                "type": "tool",
                "name": "Submit the bid",
                "config": {
                    "tool_name": "bidding.submit",
                    "parameters": {"greeting": "{{ nodes.hello.output }}"},
                },
                "save_as": "bid",
            },
            {
                "id": "after",
                "type": "transform",
                "name": "Record the outcome",
                "config": {"input": "submitted", "expression": "input"},
                "save_as": "note",
            },
        ],
        "edges": [
            {"from": "hello", "to": "review"},
            # An approval that authorizes a write declares its target instead of
            # an edge, so the compiler owns that one edge.
            {"from": "submit", "to": "after"},
        ],
    }


def _evidence_payload(
    pool: Any, tenant: dict[str, Any], execution_id: str, content: dict[str, Any]
) -> str:
    """Put a verifiable evidence record in the execution's controlled store."""
    from octop.infra.db.repos.workbuddy_runtime import WorkBuddyRuntimeRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext

    ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
    payload_id = WorkBuddyRuntimeRepo(pool).insert_payload(
        ctx,
        tenant_id=tenant["tenant_id"],
        execution_id=execution_id,
        kind="reconciliation_evidence",
        node_id="submit",
        content=content,
        sha256="0" * 64,
        size_bytes=len(str(content)),
    )
    return payload_id


@pytest.fixture
def lost_response(pool: Any, monkeypatch: Any) -> Any:
    """Route the runtime router at a service whose tool adapter loses responses."""
    from octop.api.routers import workbuddy_runtime as router_module
    from octop.infra.workbuddy.runtime import WorkBuddyRuntimeService

    port = _LostResponsePort()
    service = WorkBuddyRuntimeService(pool, effects=port)
    monkeypatch.setattr(router_module, "_service", lambda server: service)
    return port


async def _approve_then_park(
    client: httpx.AsyncClient, workflow_id: str, execution_id: str
) -> None:
    """Approve the authorization so the write is dispatched, then lose it."""
    listed = await client.get("/approval-requests")
    requests = [item for item in listed.json()["data"]["items"] if item["status"] == "pending"]
    assert requests, listed.text
    approval_id = requests[0]["id"]
    challenge = await client.post(f"/approval-requests/{approval_id}/challenge")
    assert challenge.status_code == 200, challenge.text
    approved = await client.post(
        f"/executions/{execution_id}/resume",
        json={
            "approval_request_id": approval_id,
            "decision": "approved",
            "token": challenge.json()["token"],
        },
    )
    assert approved.status_code == 200, approved.text


async def _park_execution(app: FastAPI, pool: Any, tenant: dict[str, Any]) -> str:
    """One execution whose external write may or may not have happened."""
    definition = external_write_definition([tenant["owner_member_id"]])
    workflow_id = _publish(pool, tenant, definition, "Lost response runtime")
    async with _client(app, _principal(tenant)) as client:
        accepted = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]
        await _approve_then_park(client, workflow_id, execution_id)
        parked = await client.get(f"/executions/{execution_id}")
        assert parked.json()["data"]["status"] == "waiting_reconciliation", parked.text
    return execution_id


async def test_unknown_external_write_parks_and_reconciles(
    app: FastAPI, pool: Any, tenant: dict[str, Any], lost_response: Any
) -> None:
    """T13: the write is never retried, and only evidence moves the execution."""
    execution_id = await _park_execution(app, pool, tenant)
    async with _client(app, _principal(tenant)) as client:
        detail = (await client.get(f"/executions/{execution_id}")).json()["data"]
        steps = {step["node_id"]: step for step in detail["steps"]}
        assert steps["submit"]["status"] == "waiting_reconciliation", steps
        assert detail["wait_reasons"] == ["reconciliation"], detail
        assert detail["waiting_steps"] == ["submit"], detail
        assert detail["cancel_requested"] is False, detail
        # The write was dispatched exactly once and never re-sent.
        assert len(lost_response.calls) == 1, lost_response.calls

        # A blind resume cannot move an execution with an unknown write.
        blind = await client.post(
            f"/executions/{execution_id}/resume",
            json={"approval_request_id": str(uuid.uuid4()), "decision": "approved", "token": "x"},
        )
        assert blind.status_code == 409, blind.text
        assert blind.json()["error"]["code"] == ErrorCode.RECONCILIATION_REQUIRED.value
        assert len(lost_response.calls) == 1, lost_response.calls

        evidence_ref = _evidence_payload(
            pool,
            tenant,
            execution_id,
            {"query": "provider operation lookup", "result": {"bid_id": "BID-77"}},
        )
        recorded = await client.post(
            f"/executions/{execution_id}/reconciliations",
            json={
                "step_id": "submit",
                "decision": "confirmed_success",
                "evidence_ref": evidence_ref,
                "external_reference": "provider-operation-20260918-001",
                "reason": "the provider lookup confirms the bid was accepted",
            },
        )
        assert recorded.status_code == 200, recorded.text
        settled = (await client.get(f"/executions/{execution_id}")).json()["data"]

    # The recorded result advanced the DAG; the tool was never called again.
    assert settled["status"] == "success", settled
    assert settled["outputs"]["bid"] == {"bid_id": "BID-77"}, settled
    assert settled["wait_reasons"] == [], settled
    steps = {step["node_id"]: step for step in settled["steps"]}
    assert steps["after"]["status"] == "success", steps
    assert len(lost_response.calls) == 1, lost_response.calls


async def test_reconciliation_requires_admin_and_evidence(
    app: FastAPI, pool: Any, tenant: dict[str, Any], lost_response: Any
) -> None:
    """Only a tenant admin may decide, and a decision needs valid evidence."""
    execution_id = await _park_execution(app, pool, tenant)
    body = {
        "step_id": "submit",
        "decision": "confirmed_failed",
        "evidence_ref": str(uuid.uuid4()),
        "reason": "the provider says nothing was written",
    }
    async with _client(app, _principal(tenant, role="member")) as member_client:
        refused = await member_client.post(f"/executions/{execution_id}/reconciliations", json=body)
        assert refused.status_code == 403, refused.text

    async with _client(app, _principal(tenant)) as client:
        unknown_evidence = await client.post(
            f"/executions/{execution_id}/reconciliations", json=body
        )
        assert unknown_evidence.status_code == 422, unknown_evidence.text
        assert (
            unknown_evidence.json()["error"]["code"]
            == ErrorCode.RECONCILIATION_EVIDENCE_INVALID.value
        )

        # A confirmed success without the caller's result is not evidence either.
        no_result = _evidence_payload(pool, tenant, execution_id, {"query": "lookup"})
        invalid_result = await client.post(
            f"/executions/{execution_id}/reconciliations",
            json={**body, "decision": "confirmed_success", "evidence_ref": no_result},
        )
        assert invalid_result.status_code == 422, invalid_result.text
        assert (
            invalid_result.json()["error"]["code"]
            == ErrorCode.RECONCILIATION_EVIDENCE_INVALID.value
        )

        still_parked = (await client.get(f"/executions/{execution_id}")).json()["data"]

    assert still_parked["status"] == "waiting_reconciliation", still_parked


async def test_confirmed_failure_terminates_without_retrying(
    app: FastAPI, pool: Any, tenant: dict[str, Any], lost_response: Any
) -> None:
    """A confirmed failure ends the branch; nothing is dispatched again."""
    execution_id = await _park_execution(app, pool, tenant)
    evidence_ref = _evidence_payload(
        pool, tenant, execution_id, {"query": "provider lookup", "result": None}
    )
    async with _client(app, _principal(tenant)) as client:
        recorded = await client.post(
            f"/executions/{execution_id}/reconciliations",
            json={
                "step_id": "submit",
                "decision": "confirmed_failed",
                "evidence_ref": evidence_ref,
                "reason": "the provider confirms the bid was never received",
            },
        )
        assert recorded.status_code == 200, recorded.text
        settled = (await client.get(f"/executions/{execution_id}")).json()["data"]

    assert settled["status"] == "failed", settled
    steps = {step["node_id"]: step for step in settled["steps"]}
    assert steps["submit"]["status"] == "failed", steps
    assert steps["after"]["status"] == "skipped", steps
    assert steps["after"]["skip_reason"] == "upstream_failed", steps
    assert len(lost_response.calls) == 1, lost_response.calls


async def test_cancel_during_unknown_write_waits_for_evidence(
    app: FastAPI, pool: Any, tenant: dict[str, Any], lost_response: Any
) -> None:
    """Cancelling cannot undo a maybe-write: it converges after reconciliation."""
    execution_id = await _park_execution(app, pool, tenant)
    async with _client(app, _principal(tenant)) as client:
        canceled = await client.post(f"/executions/{execution_id}/cancel")
        assert canceled.status_code == 202, canceled.text
        pending = (await client.get(f"/executions/{execution_id}")).json()["data"]
        assert pending["status"] == "waiting_reconciliation", pending
        assert pending["cancel_requested"] is True, pending

        evidence_ref = _evidence_payload(
            pool, tenant, execution_id, {"query": "provider lookup", "result": None}
        )
        recorded = await client.post(
            f"/executions/{execution_id}/reconciliations",
            json={
                "step_id": "submit",
                "decision": "confirmed_failed",
                "evidence_ref": evidence_ref,
                "reason": "the provider confirms nothing was written",
            },
        )
        assert recorded.status_code == 200, recorded.text
        settled = (await client.get(f"/executions/{execution_id}")).json()["data"]

    assert settled["status"] == "canceled", settled
    assert settled["cancel_requested"] is True, settled
    assert len(lost_response.calls) == 1, lost_response.calls


class _UsagePort:
    """A model adapter that reports what the call spent, in the platform's shape."""

    def __init__(self, *, total_tokens: int) -> None:
        self.total_tokens = total_tokens
        self.calls = 0

    def execute_llm(self, *, node: Any, activation: Any) -> Any:
        self.calls += 1
        return {"text": "hello", "usage": {"total_tokens": self.total_tokens}}

    def execute_tool(
        self, *, node: Any, activation: Any, idempotency_key: str
    ) -> Any:  # pragma: no cover
        raise AssertionError("this workflow has no tool node")

    def respond_chat(
        self, *, session_id: str, message: str, history: Any
    ) -> Any:  # pragma: no cover
        raise AssertionError("chat is not part of this workflow")


def llm_definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string", "required": True, "default": "world"}},
        "nodes": [
            {
                "id": "summarise",
                "type": "llm",
                "name": "Summarise",
                "config": {"model": "test-model", "prompt": "summarise {{ inputs.who }}"},
                "save_as": "summary",
            }
        ],
        "edges": [],
    }


async def test_model_tokens_are_recorded_for_the_metrics(
    app: FastAPI, pool: Any, tenant: dict[str, Any], monkeypatch: Any
) -> None:
    """The canary token metric needs a source: what the adapter reported."""
    from octop.api.routers import workbuddy_runtime as router_module
    from octop.infra.workbuddy.runtime import WorkBuddyRuntimeService

    port = _UsagePort(total_tokens=42)
    service = WorkBuddyRuntimeService(pool, effects=port)
    monkeypatch.setattr(router_module, "_service", lambda server: service)

    workflow_id = _publish(pool, tenant, llm_definition(), "Token runtime")
    async with _client(app, _principal(tenant)) as client:
        accepted = await client.post(
            f"/workflows/{workflow_id}/execute", json={"inputs": {"who": "tokens"}}
        )
        assert accepted.status_code == 202, accepted.text
        execution = accepted.json()["data"]

    assert port.calls == 1, port.calls
    assert execution["status"] == "success", execution
    assert execution["token_usage"] == 42, execution
    assert execution["active_duration_ms"] >= 0, execution


async def test_a_superseded_runner_cannot_commit(pool: Any, tenant: dict[str, Any]) -> None:
    """T13: fencing, not politeness, is what stops the old worker."""
    from octop.infra.db.repos.workbuddy_runtime import WorkBuddyRuntimeRepo
    from octop.infra.db.workbuddy_context import WorkBuddyDbContext

    ctx = WorkBuddyDbContext.for_tenant(tenant["tenant_id"], user_id=tenant["owner_user_id"])
    repo = WorkBuddyRuntimeRepo(pool)
    lease = f"execution:{uuid.uuid4()}"
    first = repo.acquire_lease(
        ctx,
        tenant_id=tenant["tenant_id"],
        lease_name=lease,
        holder="worker-a",
        ttl_seconds=1,
    )
    assert first is not None, first
    # While the lease is live, nobody else may take it.
    assert (
        repo.acquire_lease(
            ctx,
            tenant_id=tenant["tenant_id"],
            lease_name=lease,
            holder="worker-b",
            ttl_seconds=60,
        )
        is None
    ), "a live lease must not be taken over"

    # Once it has expired, another runner takes over and the fence moves on.
    time.sleep(1.2)
    second = repo.acquire_lease(
        ctx,
        tenant_id=tenant["tenant_id"],
        lease_name=lease,
        holder="worker-b",
        ttl_seconds=60,
    )
    assert second is not None and second > first, (first, second)

    # The old holder can neither commit nor keep the lease alive.
    assert (
        repo.verify_fence(
            ctx,
            tenant_id=tenant["tenant_id"],
            lease_name=lease,
            holder="worker-a",
            fence=first,
        )
        is False
    ), "a superseded fence must not verify"
    assert (
        repo.verify_fence(
            ctx,
            tenant_id=tenant["tenant_id"],
            lease_name=lease,
            holder="worker-b",
            fence=second,
        )
        is True
    ), "the current holder keeps its fence"


async def test_a_token_does_not_make_you_an_approver(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T09: holding the one-time token is not enough; the candidate list decides."""
    definition = approval_definition([tenant["owner_member_id"]])
    workflow_id = _publish(pool, tenant, definition, "Approver candidate runtime")
    async with _client(app, _principal(tenant)) as client:
        accepted = await client.post(f"/workflows/{workflow_id}/execute", json={"inputs": {}})
        assert accepted.status_code == 202, accepted.text
        execution_id = accepted.json()["data"]["id"]
        listed = await client.get("/approval-requests")
        request_item = listed.json()["data"]["items"][0]
        # The candidate (the owner) issues the token; a second member tries to use it.
        challenge = await client.post(f"/approval-requests/{request_item['id']}/challenge")
        assert challenge.status_code == 200, challenge.text
        token = challenge.json()["token"]

    outsider = _principal(
        tenant,
        user_id=tenant["reviewer_user_id"],
        role="member",
        member_id=tenant["reviewer_member_id"],
    )
    async with _client(app, outsider) as client:
        refused = await client.post(
            f"/executions/{execution_id}/resume",
            json={
                "approval_request_id": request_item["id"],
                "decision": "approved",
                "token": token,
            },
        )
        # A non-candidate cannot even see the request, so the refusal is the
        # uniform not-found rather than a distinction the tenant could probe.
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value, refused.text

    async with _client(app, _principal(tenant)) as client:
        settled = await client.get(f"/executions/{execution_id}")
    assert settled.json()["data"]["status"] == "waiting_approval", settled.text

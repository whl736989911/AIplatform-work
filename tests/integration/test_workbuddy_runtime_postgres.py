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
    tenant_row = repo.create_tenant(
        f"rt-{uuid.uuid4().hex[:8]}", "Runtime tenant", owner_user_id=owner_id
    )
    owner_member = repo.list_members(tenant_row["tenant_id"])[0]
    return {
        "tenant_id": tenant_row["tenant_id"],
        "slug": tenant_row["slug"],
        "owner_user_id": owner_id,
        "owner_member_id": owner_membership_id(owner_member),
    }


def owner_membership_id(member: dict[str, Any]) -> str:
    return str(member.get("membership_id") or member.get("id"))


def _principal(tenant: dict[str, Any], *, role: str = "owner") -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(
            id=tenant["owner_user_id"],
            username=f"rt-{tenant['tenant_id'][:8]}",
            role=Role.USER,
            display_name=None,
        ),
        tenant_id=tenant["tenant_id"],
        tenant_slug=tenant["slug"],
        tenant_name="Runtime tenant",
        member_id=tenant["owner_member_id"],
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
            taken, untaken = (
                ("yes", "no") if expected == "accepted" else ("no", "yes")
            )
            assert steps[taken]["status"] == "success", steps
            assert steps[untaken]["status"] == "skipped", steps
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
        assert conflict.json()["error"]["code"] == ErrorCode.WORKBUDDY_IDEMPOTENCY_CONFLICT.value


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
        assert (
            cancelled.json()["error"]["code"]
            == ErrorCode.WORKBUDDY_EXECUTION_NOT_CANCELLABLE.value
        )


async def test_suspended_tenant_cannot_start_new_executions(
    app: FastAPI, pool: Any, tenant: dict[str, Any]
) -> None:
    """T18: suspension blocks new starts; the fact store is the authority."""
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    workflow_id = _publish(pool, tenant, hello_definition(), "Suspended runtime")
    principal = _principal(tenant)
    repo = WorkBuddyIdentityRepo(pool)
    try:
        repo.suspend_tenant(tenant["tenant_id"], reason="billing review")
        suspended = _principal({**tenant})
        async with _client(app, suspended) as client:
            refused = await client.post(
                f"/workflows/{workflow_id}/execute", json={"inputs": {}}
            )
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] in {
            ErrorCode.WORKBUDDY_TENANT_SUSPENDED.value,
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

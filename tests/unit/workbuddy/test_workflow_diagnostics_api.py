"""The validate route must hand a client every diagnostic, not just the first.

``_refusal`` maps a compiler code onto an :class:`ErrorCode`, and most compiler
codes collapse onto the generic ``WF_INVALID_SCHEMA``; the per-defect list would
disappear with the code if it stayed only on the exception.  These tests drive
the real route through a stubbed transaction (no PostgreSQL) and assert on the
response body a client actually parses.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.deps import get_server
from octop.api.routers import workbuddy_workflows
from octop.api.routers.workbuddy_identity import workbuddy_principal
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbuddy.workflow_compiler import (
    WORKFLOW_EDGE_UNKNOWN_NODE,
    WORKFLOW_REFERENCE_UNKNOWN,
    WORKFLOW_TOOL_UNAVAILABLE,
    WorkflowCompileError,
)

TENANT = "6f1a2c3d-4e5b-4a7c-8d9e-0f1a2b3c4d5e"


class _Principal:
    """The route only reads the derived tenant and user id."""

    def __init__(self, tenant_id: str = TENANT, user_id: int = 7) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.user = SimpleNamespace(id=user_id, username="wb-member")

    @property
    def member_id(self) -> str:
        return "member-7"


class _Cursor:
    """A query that proves nothing: the tenant catalog is empty at this point."""

    def fetchone(self) -> None:
        return None


def _definition() -> dict[str, Any]:
    """Two broken references in two different nodes, both provable in one pass."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "inputs": {"who": {"type": "string"}},
        "nodes": [
            {
                "id": "greet",
                "type": "transform",
                "name": "Greet",
                "config": {"input": {"text": "{{ inputs.missing }}"}, "expression": "inputs"},
            },
            {
                "id": "farewell",
                "type": "transform",
                "name": "Farewell",
                "config": {"input": {"text": "{{ nodes.ghost.output }}"}, "expression": "inputs"},
            },
        ],
        "edges": [{"from": "greet", "to": "farewell"}],
    }


@pytest.fixture
def contexts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Open the tenant transaction without a database and record its scopes."""
    seen: list[Any] = []

    @contextmanager
    def _transaction(db: Any, ctx: Any = None) -> Any:
        seen.append(ctx)
        yield SimpleNamespace(execute=lambda *args, **kwargs: _Cursor())

    monkeypatch.setattr(workbuddy_workflows, "workbuddy_transaction", _transaction)
    return seen


def _client(principal: Any = None) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(workbuddy_workflows.router, prefix="/api/v1")

    @app.exception_handler(OctopError)
    async def _octop(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    server = SimpleNamespace(services=SimpleNamespace(db=object()))
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[workbuddy_principal] = lambda: principal or _Principal()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_validate_refusal_carries_every_diagnostic(contexts: list[Any]) -> None:
    async with _client() as client:
        response = await client.post(
            "/api/v1/workflow-definitions/validate", json={"definition": _definition()}
        )

    assert response.status_code == 422
    error = response.json()["error"]
    # The coarse API code is unchanged: no new ErrorCode was minted for this.
    assert error["code"] == ErrorCode.WF_INVALID_SCHEMA.value
    details = error["details"]
    assert details["compiler_code"] == WORKFLOW_REFERENCE_UNKNOWN
    assert [item["code"] for item in details["diagnostics"]] == [
        WORKFLOW_REFERENCE_UNKNOWN,
        WORKFLOW_REFERENCE_UNKNOWN,
    ]
    assert [item["path"] for item in details["diagnostics"]] == [
        "nodes.farewell.config.input",
        "nodes.greet.config.input",
    ]
    # ``node_id`` and ``hint_key`` are what an editor groups and localizes by.
    assert [item["node_id"] for item in details["diagnostics"]] == ["farewell", "greet"]
    assert details["diagnostics"][0]["hint_key"] == (
        f"workflowDiagnostics.{WORKFLOW_REFERENCE_UNKNOWN}"
    )
    assert details["stage"] == "references"
    # The transaction ran under the caller's tenant, never a body-supplied one.
    assert [str(ctx.tenant_id) for ctx in contexts] == [TENANT]
    assert [ctx.user_id for ctx in contexts] == [7]


async def test_validate_downgrades_an_unmapped_compiler_code(contexts: list[Any]) -> None:
    """A refused tool keeps its own code in ``details`` and stays a 422."""
    async with _client() as client:
        response = await client.post(
            "/api/v1/workflow-definitions/validate",
            json={
                "definition": {
                    "schema_version": 1,
                    "trigger": {"type": "manual", "config": {}},
                    "nodes": [
                        {
                            "id": "call",
                            "type": "tool",
                            "name": "Call",
                            "config": {"tool_name": "not_installed", "parameters": {}},
                        }
                    ],
                    "edges": [],
                }
            },
        )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == ErrorCode.WF_INVALID_SCHEMA.value
    assert error["details"]["compiler_code"] == WORKFLOW_TOOL_UNAVAILABLE
    assert [item["code"] for item in error["details"]["diagnostics"]] == [WORKFLOW_TOOL_UNAVAILABLE]


def test_refusal_maps_an_unknown_compiler_code_to_the_validation_error() -> None:
    """A store/compiler code with no ErrorCode member is a 422, never a 500."""
    exc = WorkflowCompileError(WORKFLOW_EDGE_UNKNOWN_NODE, "edge target 'ghost' is not a node")

    refused = workbuddy_workflows._refusal(exc)

    assert refused.code == ErrorCode.WF_INVALID_SCHEMA
    assert refused.details["compiler_code"] == WORKFLOW_EDGE_UNKNOWN_NODE
    assert refused.details["diagnostics"] == [
        {
            "code": WORKFLOW_EDGE_UNKNOWN_NODE,
            "message": "edge target 'ghost' is not a node",
            "hint_key": f"workflowDiagnostics.{WORKFLOW_EDGE_UNKNOWN_NODE}",
        }
    ]


def test_refusal_keeps_a_code_that_names_an_error_code() -> None:
    """A compiler code that is an ``ErrorCode`` member must not be downgraded."""
    exc = WorkflowCompileError(ErrorCode.APPROVAL_NO_VALID_APPROVER.value, "no valid approver")

    refused = workbuddy_workflows._refusal(exc)

    assert refused.code is ErrorCode.APPROVAL_NO_VALID_APPROVER
    assert refused.details["compiler_code"] == ErrorCode.APPROVAL_NO_VALID_APPROVER.value
    assert refused.details["diagnostics"][0]["code"] == ErrorCode.APPROVAL_NO_VALID_APPROVER.value

"""The version-comparison route must describe a real change, not a rewrite.

A workflow definition is a JSON document whose node lists carry stable ``id``
values, so the route reuses the keyed semantic diff proposals already trust:
reordering nodes is not a change, editing one field is, and the reported paths
are the JSON pointers a client groups by.  These tests drive the real route
through a stubbed repository (no PostgreSQL) and assert on the body a client
parses — including the refusals, because an empty change list that only looks
like "nothing changed" would be worse than an error.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.deps import get_server
from octop.api.routers import workbuddy_workflows
from octop.api.routers.workbuddy_identity import workbuddy_principal
from octop.infra.db.repos.workbuddy_workflows import WorkflowRecord, WorkflowVersionRecord
from octop.infra.errors import ErrorCode, OctopError

MANIFEST = Path(__file__).resolve().parents[3] / "contracts" / "route-manifest.json"

TENANT = "6f1a2c3d-4e5b-4a7c-8d9e-0f1a2b3c4d5e"
OWNER_USER_ID = 7
WORKFLOW_ID = "1f0f7f34-0b7a-4f2f-9a86-4a4ac1b9f001"
OTHER_WORKFLOW_ID = "2f0f7f34-0b7a-4f2f-9a86-4a4ac1b9f002"
VERSION_1 = "aa0f7f34-0b7a-4f2f-9a86-4a4ac1b9f101"
VERSION_2 = "aa0f7f34-0b7a-4f2f-9a86-4a4ac1b9f102"
VERSION_3 = "aa0f7f34-0b7a-4f2f-9a86-4a4ac1b9f103"
FOREIGN_VERSION = "bb0f7f34-0b7a-4f2f-9a86-4a4ac1b9f201"

_GREET = {"id": "greet", "type": "transform", "name": "Greet", "config": {"expression": "inputs"}}
_FAREWELL = {
    "id": "farewell",
    "type": "transform",
    "name": "Farewell",
    "config": {"expression": "inputs"},
}
_AUDIT = {"id": "audit", "type": "transform", "name": "Audit", "config": {"expression": "inputs"}}
_EDGE = {"from": "greet", "to": "farewell"}


def _definition(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """A minimal valid shape; the route compares it and never compiles it."""
    return {
        "schema_version": 1,
        "trigger": {"type": "manual", "config": {}},
        "nodes": nodes,
        "edges": edges,
    }


def _base() -> dict[str, Any]:
    return _definition([dict(_GREET), dict(_FAREWELL)], [dict(_EDGE)])


def _edited() -> dict[str, Any]:
    """Renamed node, one added node, no edges, and the surviving nodes reordered."""
    renamed = {**_GREET, "name": "Greet v2"}
    return _definition([dict(_FAREWELL), dict(_AUDIT), renamed], [])


def _reordered() -> dict[str, Any]:
    """The same document as :func:`_base` with its nodes swapped."""
    return _definition([dict(_FAREWELL), dict(_GREET)], [dict(_EDGE)])


def _sha256(definition: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode("utf-8")).hexdigest()


class _Principal:
    """The route only reads the derived tenant, user, and membership identity."""

    def __init__(self, *, user_id: int = OWNER_USER_ID, is_admin: bool = False) -> None:
        self.tenant_id = TENANT
        self.user_id = user_id
        self.user = SimpleNamespace(id=user_id, username="wb-owner")
        self.is_admin = is_admin

    @property
    def member_id(self) -> str:
        return f"member-{self.user_id}"


def _workflow(workflow_id: str = WORKFLOW_ID) -> WorkflowRecord:
    return WorkflowRecord(
        tenant_id=TENANT,
        workflow_id=workflow_id,
        name="Weekly report",
        description=None,
        status="active",
        revision=3,
        active_version_id=VERSION_2,
        shadow_version_id=None,
        created_by=OWNER_USER_ID,
        created_by_membership_id=f"member-{OWNER_USER_ID}",
        created_at=1_700_000_000,
        updated_at=1_700_000_500,
        archived_at=None,
    )


def _version(
    version_id: str,
    number: int,
    definition: dict[str, Any],
    *,
    workflow_id: str = WORKFLOW_ID,
    origin: str = "manual",
) -> WorkflowVersionRecord:
    return WorkflowVersionRecord(
        tenant_id=TENANT,
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        version_number=number,
        definition=definition,
        definition_sha256=_sha256(definition),
        origin=origin,
        base_version_id=None,
        source_version_id=None,
        change_summary=None,
        created_by=OWNER_USER_ID,
        created_by_membership_id=f"member-{OWNER_USER_ID}",
        created_at=1_700_000_000 + number,
    )


class _Repo:
    """Only the two reads the version routes make; anything else is unreachable."""

    def __init__(self, workflow: WorkflowRecord, versions: list[WorkflowVersionRecord]) -> None:
        self._workflow = workflow
        self._versions = {version.workflow_version_id: version for version in versions}

    def get_workflow(
        self, tenant_id: str, workflow_id: str, *, conn: Any | None = None
    ) -> WorkflowRecord | None:
        del tenant_id, conn
        return self._workflow if workflow_id == self._workflow.workflow_id else None

    def get_version(
        self,
        tenant_id: str,
        workflow_id: str,
        version_id: str,
        *,
        conn: Any | None = None,
    ) -> WorkflowVersionRecord | None:
        del tenant_id, conn
        version = self._versions.get(version_id)
        if version is None or version.workflow_id != workflow_id:
            return None
        return version


@pytest.fixture(autouse=True)
def repo(monkeypatch: pytest.MonkeyPatch) -> _Repo:
    """Three versions of the workflow, plus one belonging to another workflow."""
    fake = _Repo(
        _workflow(),
        [
            _version(VERSION_1, 1, _base()),
            _version(VERSION_2, 2, _edited(), origin="proposal"),
            _version(VERSION_3, 3, _reordered()),
            _version(FOREIGN_VERSION, 1, _base(), workflow_id=OTHER_WORKFLOW_ID),
        ],
    )
    monkeypatch.setattr(workbuddy_workflows, "_repo", lambda server: fake)
    return fake


def _client(principal: Any = None) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(workbuddy_workflows.router, prefix="/api/v1")

    @app.exception_handler(OctopError)
    async def _octop(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    app.dependency_overrides[get_server] = lambda: SimpleNamespace(
        services=SimpleNamespace(db=object())
    )
    app.dependency_overrides[workbuddy_principal] = lambda: principal or _Principal()
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _diff_url(*, source: str | None = VERSION_1, target: str | None = VERSION_2) -> str:
    query = "&".join(
        f"{name}={value}" for name, value in (("from", source), ("to", target)) if value is not None
    )
    return f"/api/v1/workflows/{WORKFLOW_ID}/versions/diff?{query}"


async def test_diff_reports_keyed_paths_and_counts() -> None:
    """One rename, one added node, one dropped edge — and a reorder that is silent."""
    async with _client() as client:
        response = await client.get(_diff_url())

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workflow_id"] == WORKFLOW_ID
    assert data["from"] == {
        "version_id": VERSION_1,
        "version_number": 1,
        "definition_sha256": _sha256(_base()),
        "origin": "manual",
    }
    assert data["to"] == {
        "version_id": VERSION_2,
        "version_number": 2,
        "definition_sha256": _sha256(_edited()),
        "origin": "proposal",
    }
    # The comparison names changes; it never ships both whole definitions.
    assert "definition" not in data["from"]
    assert {(change["path"], change["kind"]) for change in data["changes"]} == {
        ("/nodes/greet/name", "replaced"),
        ("/nodes/audit", "added"),
        ("/edges/0", "removed"),
    }
    assert data["summary"] == {"added": 1, "removed": 1, "replaced": 1}
    replaced = next(change for change in data["changes"] if change["kind"] == "replaced")
    assert replaced["old"] == "Greet"
    assert replaced["new"] == "Greet v2"
    assert len(data["changes"]) == 3


async def test_reordering_nodes_alone_is_not_a_change() -> None:
    """The documents differ byte for byte, and still compare as unchanged."""
    async with _client() as client:
        response = await client.get(_diff_url(source=VERSION_1, target=VERSION_3))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["from"]["definition_sha256"] != data["to"]["definition_sha256"]
    assert data["changes"] == []
    assert data["summary"] == {"added": 0, "removed": 0, "replaced": 0}


async def test_comparing_a_version_with_itself_is_refused() -> None:
    """An empty diff would claim "nothing changed" without comparing anything."""
    async with _client() as client:
        response = await client.get(_diff_url(source=VERSION_1, target=VERSION_1))
        # The ids are compared as UUIDs, so a differently-cased spelling of one
        # id is still the same version, not a comparison of two.
        recased = await client.get(_diff_url(source=VERSION_1, target=VERSION_1.upper()))

    for refused in (response, recased):
        assert refused.status_code == 400
        error = refused.json()["error"]
        assert error["code"] == ErrorCode.WORKBUDDY_INVALID_ARGUMENT.value
        assert "different" in error["message"]


async def test_a_missing_version_parameter_is_a_validation_error() -> None:
    async with _client() as client:
        missing_to = await client.get(_diff_url(target=None))
        missing_from = await client.get(_diff_url(source=None))
        missing_both = await client.get(_diff_url(source=None, target=None))

    assert [missing_to.status_code, missing_from.status_code, missing_both.status_code] == [
        422,
        422,
        422,
    ]


async def test_an_unknown_or_foreign_version_is_a_uniform_404() -> None:
    """A version of another workflow, an unknown id and a malformed id all 404."""
    unknown = "cc0f7f34-0b7a-4f2f-9a86-4a4ac1b9f301"
    async with _client() as client:
        foreign = await client.get(_diff_url(source=VERSION_1, target=FOREIGN_VERSION))
        unknown_target = await client.get(_diff_url(source=VERSION_1, target=unknown))
        malformed = await client.get(_diff_url(source="not-a-uuid", target=VERSION_2))

    assert [foreign.status_code, unknown_target.status_code, malformed.status_code] == [
        404,
        404,
        404,
    ]
    assert foreign.json()["error"]["message"] == "workflow version not found"
    assert malformed.json()["error"]["message"] == "workflow version not found"


async def test_a_member_who_does_not_own_the_workflow_sees_the_uniform_404() -> None:
    """Membership is not enough: the diff is a management view, like the versions list."""
    async with _client(_Principal(user_id=9)) as client:
        response = await client.get(_diff_url())

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "workflow not found"


def test_the_manifest_and_the_router_both_publish_the_diff_route() -> None:
    """The frozen contract is bidirectional: dropping either side must fail here."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    published = {
        (entry["method"], entry["path"], entry["success_status"]) for entry in manifest["routes"]
    }
    assert ("GET", "/workflows/{id}/versions/diff", 200) in published

    registered = {
        (method, route.path, getattr(route, "status_code", None) or 200)
        for route in workbuddy_workflows.router.routes
        for method in getattr(route, "methods", []) or []
    }
    assert ("GET", "/workflows/{workflow_id}/versions/diff", 200) in registered

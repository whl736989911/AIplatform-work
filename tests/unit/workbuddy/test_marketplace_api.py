"""Focused unit tests for the WorkBuddy marketplace router (no live PostgreSQL).

The router is driven through a minimal HTTP app with an in-memory marketplace
store, so the frozen HTTP contract and the refusals the product contract names by
hand are proven without a database: a submission carrying live credential
material is rejected before anything is persisted, an install whose consent does
not cover the published version never starts, and another member's resources are
a uniform 404.  The database-side guarantees (immutable published versions,
consent evidence, compare-and-swap revisions) live in the PostgreSQL gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from octop.api.deps import get_server
from octop.api.routers import workbuddy_marketplace as api
from octop.api.routers.workbuddy_identity import (
    WorkBuddyPlatformPrincipal,
    WorkBuddyPrincipal,
    workbuddy_principal,
)
from octop.infra.db.workbuddy_context import WorkBuddyPostgresRequiredError
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User
from octop.infra.workbuddy import marketplace as M

TENANT = "11111111-1111-4111-8111-111111111111"
MEMBER = "22222222-2222-4222-8222-222222222222"
OTHER_MEMBER = "33333333-3333-4333-8333-333333333333"
TEMPLATE = "44444444-4444-4444-8444-444444444444"
TEMPLATE_VERSION = "55555555-5555-4555-8555-555555555555"
SUBMISSION_ID = "66666666-6666-4666-8666-666666666666"
INSTALLATION_ID = "77777777-7777-4777-8777-777777777777"
KB_ID = "88888888-8888-4888-8888-888888888888"
WORKFLOW_ID = "99999999-9999-4999-8999-999999999999"
WORKFLOW_VERSION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
JOB_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
UPGRADE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
REVIEW_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
KB_SLOT = "policy_kb"
PLACEHOLDER = M.placeholder_uuid(1)
PUBLISHER_LICENSE = "Publish terms: sanitized templates only."
PRIVATE_KEY_BLOCK = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
)


def template_definition() -> dict[str, Any]:
    """A minimal reviewed template: one transform node, no bindings."""
    return {
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


def published_version(**overrides: Any) -> dict[str, Any]:
    """The published-version row shape the service projects for an install."""
    definition = template_definition()
    row: dict[str, Any] = {
        "template_id": TEMPLATE,
        "template_version_id": TEMPLATE_VERSION,
        "version": "1.0.0",
        "definition": definition,
        "definition_hash": M.definition_sha256(definition),
        "license_id": "octop-community",
        "license_text_hash": M.sha256_hex(PUBLISHER_LICENSE),
        "required_capabilities": [],
        "content_summary": "Greets the caller.",
        "publisher_display": "Octop Labs",
    }
    row.update(overrides)
    return row


def consent_body(**overrides: Any) -> dict[str, Any]:
    """A consent payload that exactly matches :func:`published_version`."""
    body: dict[str, Any] = {
        "accepted": True,
        "template_version_id": TEMPLATE_VERSION,
        "license_text_hash": M.sha256_hex(PUBLISHER_LICENSE),
        "capabilities_hash": M.sha256_hex(M.canonical_json([])),
    }
    body.update(overrides)
    return body


_MISSING = object()


class _Catalog:
    """Capability gate stand-in: no approved revisions, no bindings required."""

    def tenant_capabilities(self, tenant_id: str) -> Any:
        return M.TenantCapabilities(tenant_id=tenant_id)

    def tool_keys(self, tenant_id: str) -> dict[str, str]:
        return {}

    def model_keys(self, tenant_id: str) -> dict[str, str]:
        return {}


class _Bindings:
    """Same-tenant existence answers for the declared rebinding slots."""

    def __init__(self, *, knowledge_bases: set[str] | frozenset[str] = frozenset()) -> None:
        self.knowledge_bases = set(knowledge_bases)

    def knowledge_base_exists(self, tenant_id: str, knowledge_base_id: str) -> bool:
        return knowledge_base_id in self.knowledge_bases

    def approver_is_active_member(self, tenant_id: str, member_id: str) -> bool:
        return False

    def credential_is_active(self, tenant_id: str, credential_id: str) -> bool:
        return False


class _Workflows:
    """Records the workflow versions an install or an upgrade asks for."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def _ref(self, definition: Mapping[str, Any], **kwargs: Any) -> Any:
        self.created.append({"definition": definition, **kwargs})
        return M.WorkflowVersionRef(
            workflow_id=WORKFLOW_ID,
            workflow_version_id=WORKFLOW_VERSION_ID,
            version_number=len(self.created),
        )

    def create_workflow_version(self, **kwargs: Any) -> Any:
        return self._ref(kwargs.pop("definition"), **kwargs)

    def append_workflow_version(self, **kwargs: Any) -> Any:
        return self._ref(kwargs.pop("definition"), **kwargs)


class _Store:
    """In-memory marketplace store covering the read and refusal paths."""

    def __init__(self, *, published: Any = _MISSING) -> None:
        self.published = published_version() if published is _MISSING else published
        self.submissions: dict[str, dict[str, Any]] = {}
        self.installations: dict[str, dict[str, Any]] = {}
        self.reviews: dict[str, list[dict[str, Any]]] = {}
        self.persisted: list[tuple[str, dict[str, Any]]] = []
        self.frozen: list[dict[str, Any]] = []
        self.consents: list[dict[str, Any]] = []
        self.upgrades: dict[str, dict[str, Any]] = {}

    # -- reads ------------------------------------------------------------- #

    def list_published_templates(
        self, *, industry: Any = None, limit: Any = None, offset: Any = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "template_id": TEMPLATE,
                "template_version_id": TEMPLATE_VERSION,
                "version": "1.0.0",
                "industry": "general",
                "summary": "Greets the caller.",
                "publisher_display": "Octop Labs",
            }
        ]

    def get_published_version(self, template_id: Any, version_id: Any) -> dict[str, Any] | None:
        if self.published is None:
            return None
        if str(template_id).lower() != TEMPLATE or str(version_id).lower() != TEMPLATE_VERSION:
            return None
        return dict(self.published)

    def get_submission(self, tenant_id: Any, submission_id: Any, **kwargs: Any) -> dict | None:
        row = self.submissions.get(str(submission_id).lower())
        return dict(row) if row is not None else None

    def list_submission_reviews(self, tenant_id: Any, submission_id: Any, **kwargs: Any) -> list:
        return list(self.reviews.get(str(submission_id).lower(), ()))

    def get_installation(self, tenant_id: Any, installation_id: Any, **kwargs: Any) -> dict | None:
        row = self.installations.get(str(installation_id).lower())
        return dict(row) if row is not None else None

    # -- writes ------------------------------------------------------------ #

    def create_submission(self, tenant_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.persisted.append(("create_submission", kwargs))
        raise AssertionError("a refused submission must never reach the store")

    def freeze_submission(self, tenant_id: Any, submission_id: Any, **kwargs: Any) -> dict:
        row = self.submissions[str(submission_id).lower()]
        frozen = {**row, "status": "submitted", "revision": int(row.get("revision", 0)) + 1}
        self.submissions[str(submission_id).lower()] = frozen
        self.frozen.append(kwargs)
        return dict(frozen)

    def create_installation(self, tenant_id: Any, **kwargs: Any) -> dict[str, Any]:
        row = {"id": INSTALLATION_ID, "revision": 1, **kwargs}
        self.installations[INSTALLATION_ID] = row
        self.persisted.append(("create_installation", kwargs))
        return dict(row)

    def record_credential_bindings(
        self, tenant_id: Any, installation_id: Any, bindings: Mapping[str, str], **kwargs: Any
    ) -> list[dict[str, Any]]:
        return [{"slot": slot, "credential_id": value} for slot, value in dict(bindings).items()]

    def record_consent(self, tenant_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.consents.append(kwargs)
        return dict(kwargs)

    def create_job(self, tenant_id: Any, **kwargs: Any) -> dict[str, Any]:
        return {"id": JOB_ID, **kwargs}

    def update_installation(self, tenant_id: Any, installation_id: Any, **kwargs: Any) -> dict:
        row = dict(self.installations[str(installation_id)])
        row.update(kwargs)
        row["revision"] = int(row.get("revision", 1)) + 1
        self.installations[str(installation_id)] = row
        return dict(row)

    def update_job(self, tenant_id: Any, job_id: Any, **kwargs: Any) -> dict[str, Any]:
        return {"id": job_id, **kwargs}

    def create_upgrade(self, tenant_id: Any, **kwargs: Any) -> dict[str, Any]:
        row = {"id": UPGRADE_ID, "revision": 1, **kwargs}
        self.upgrades[UPGRADE_ID] = row
        return dict(row)

    def update_upgrade(self, tenant_id: Any, upgrade_id: Any, **kwargs: Any) -> dict:
        row = {**self.upgrades.get(str(upgrade_id), {"id": upgrade_id}), **kwargs}
        self.upgrades[str(upgrade_id)] = row
        return dict(row)

    def publish_template_version(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE,
            "template_version_id": TEMPLATE_VERSION,
            "version": kwargs.get("version"),
            "definition_hash": kwargs.get("definition_hash"),
        }

    def decide_submission(
        self, tenant_id: Any, submission_id: Any, **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        row = dict(self.submissions[str(submission_id).lower()])
        row["status"] = str(kwargs.get("decision"))
        row["published_template_version_id"] = kwargs.get("published_template_version_id")
        row["revision"] = int(row.get("revision", 0)) + 1
        review = {
            "id": REVIEW_ID,
            "submission_id": submission_id,
            "decision": kwargs.get("decision"),
        }
        self.submissions[str(submission_id).lower()] = row
        return dict(row), review


class _InstallationLookup:
    """The repository call the upgrade route makes before it reaches the service."""

    def __init__(self, installation: Mapping[str, Any] | None) -> None:
        self._installation = installation

    def get_installation(self, tenant_id: Any, installation_id: Any) -> dict[str, Any] | None:
        return None if self._installation is None else dict(self._installation)


class _UnusedPorts:
    """Fails loudly if a refusal path ever consults the tenant's configuration."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"refused before the service used the {name} port")


class _NonPostgresStore(_Store):
    """A store backed by a SQLite control plane (migration 021 fails closed)."""

    def list_published_templates(self, **kwargs: Any) -> list[dict[str, Any]]:
        raise WorkBuddyPostgresRequiredError("WorkBuddy requires a PostgreSQL database")


def _principal(
    *,
    user_id: int = 10,
    role: str = "member",
    member_id: str = MEMBER,
    tenant_id: str = TENANT,
) -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"user{user_id}", role=Role.USER, display_name=None),
        tenant_id=tenant_id,
        tenant_slug="acme",
        tenant_name="Acme",
        member_id=member_id,
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


def _app(
    store: _Store,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bindings: Any = None,
    workflows: Any = None,
) -> FastAPI:
    service = M.MarketplaceService(
        store=store,
        catalog=_Catalog(),
        bindings=bindings if bindings is not None else _UnusedPorts(),
        workflows=workflows if workflows is not None else _UnusedPorts(),
        db=None,
    )
    monkeypatch.setattr(api, "_service", lambda _server: service)
    server = SimpleNamespace(services=SimpleNamespace(db=object()))
    app = FastAPI()
    app.include_router(api.router)
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
    json_body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    app.dependency_overrides[workbuddy_principal] = lambda: principal
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, json=json_body, headers=dict(headers or {}))


def _submission_body(definition: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": "Greeting template",
        "summary": "Greets the caller.",
        "industry": "general",
        "definition": definition,
        "license_id": "octop-community",
        "license_text": PUBLISHER_LICENSE,
        "capabilities": [],
    }


def placeholder_version() -> dict[str, Any]:
    """A published version that reaches one knowledge base through a placeholder."""
    definition = template_definition()
    definition["nodes"][0]["config"]["input"] = {"kb": PLACEHOLDER, "greeting": "hello"}
    return published_version(
        definition=definition,
        definition_hash=M.definition_sha256(definition),
        required_capabilities=[
            {
                "kind": "knowledge_base",
                "key": KB_SLOT,
                "label": "Policy knowledge base",
                "placeholder_id": PLACEHOLDER,
                "required": True,
            }
        ],
    )


def _install_body(version: Mapping[str, Any], *, bindings: Any = None) -> dict[str, Any]:
    return {
        "template_version_id": TEMPLATE_VERSION,
        "consent": consent_body(
            capabilities_hash=M.sha256_hex(M.canonical_json(list(version["required_capabilities"])))
        ),
        "bindings": bindings or {},
        "credential_bindings": {},
        "workflow_name": "Policy template",
    }


async def test_a_non_postgres_control_plane_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: the marketplace answers a controlled 503 where its tables cannot exist."""
    app = _app(_NonPostgresStore(), monkeypatch)
    response = await _request(
        app, "GET", "/marketplace/templates", principal=_principal(), headers={}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_POSTGRES_REQUIRED.value


async def test_browse_returns_the_reviewed_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    """T24: the catalogue endpoint answers in the frozen envelope."""
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "GET",
        "/marketplace/templates",
        principal=_principal(),
        headers={"x-request-id": "req_abc123"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == "req_abc123"
    assert body["data"][0]["template_id"] == TEMPLATE
    assert body["data"][0]["version"] == "1.0.0"


async def test_unpublished_template_version_is_a_uniform_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a version that is not published is not visible, whatever its id."""
    store = _Store(published=None)
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "GET",
        f"/marketplace/templates/{TEMPLATE}/versions/{TEMPLATE_VERSION}",
        principal=_principal(),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_MARKETPLACE_NOT_FOUND.value


async def test_install_requires_explicitly_accepted_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: consent must be accepted, and is checked before any store write."""
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body={
            "template_version_id": TEMPLATE_VERSION,
            "consent": consent_body(accepted=False),
            "bindings": {},
            "credential_bindings": {},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_CONSENT_REQUIRED.value
    assert store.persisted == []


async def test_install_refuses_consent_for_a_different_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: consent is bound to the exact published version."""
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body={
            "template_version_id": TEMPLATE_VERSION,
            "consent": consent_body(template_version_id=str(uuid.uuid4())),
            "bindings": {},
            "credential_bindings": {},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_CONSENT_REQUIRED.value
    assert store.persisted == []


async def test_install_refuses_a_licence_the_caller_did_not_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: consenting to a different licence text than the published one fails."""
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body={
            "template_version_id": TEMPLATE_VERSION,
            "consent": consent_body(license_text_hash=M.sha256_hex("some other licence")),
            "bindings": {},
            "credential_bindings": {},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_LICENSE_NOT_ACCEPTED.value
    assert store.persisted == []


async def test_submission_with_a_private_key_block_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a template carrying live credential material never reaches the store."""
    definition = template_definition()
    definition["nodes"][0]["config"]["greeting"] = PRIVATE_KEY_BLOCK
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        "/marketplace/submissions",
        principal=_principal(),
        json_body=_submission_body(definition),
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.WORKBUDDY_SUBMISSION_INVALID.value
    assert error["details"]["path"].endswith("greeting")
    # The rejection names the location and never echoes the secret material.
    assert "PRIVATE KEY" not in response.text
    assert store.persisted == []


async def test_submission_with_a_credential_shaped_field_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a credential-shaped config key is refused by name, not by luck."""
    definition = template_definition()
    definition["nodes"][0]["config"]["private_key"] = "not-a-real-key"
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        "/marketplace/submissions",
        principal=_principal(),
        json_body=_submission_body(definition),
    )
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.WORKBUDDY_SUBMISSION_INVALID.value
    assert error["details"]["path"].endswith("private_key")
    assert "not-a-real-key" not in response.text
    assert store.persisted == []


async def test_another_members_submission_is_a_uniform_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: authorship is enforced; a tenant admin may still read it."""
    store = _Store()
    store.submissions[SUBMISSION_ID] = {
        "id": SUBMISSION_ID,
        "submitted_by": MEMBER,
        "status": "draft",
        "revision": 3,
    }
    store.reviews[SUBMISSION_ID] = [{"decision": "pending", "reviewer_user_id": 99}]
    app = _app(store, monkeypatch)

    hidden = await _request(
        app,
        "GET",
        f"/marketplace/submissions/{SUBMISSION_ID}",
        principal=_principal(member_id=OTHER_MEMBER, user_id=11),
    )
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["error"]["code"] == ErrorCode.WORKBUDDY_MARKETPLACE_NOT_FOUND.value

    visible = await _request(
        app,
        "GET",
        f"/marketplace/submissions/{SUBMISSION_ID}",
        principal=_principal(role="admin"),
    )
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["reviews"] == [{"decision": "pending", "reviewer_user_id": 99}]


async def test_freezing_a_frozen_submission_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a submission that was already frozen stays immutable (409)."""
    store = _Store()
    store.submissions[SUBMISSION_ID] = {
        "id": SUBMISSION_ID,
        "submitted_by": MEMBER,
        "status": "submitted",
        "revision": 3,
    }
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/marketplace/submissions/{SUBMISSION_ID}/submit",
        principal=_principal(),
        json_body={"expected_revision": 3},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == ErrorCode.WORKBUDDY_SUBMISSION_FROZEN.value
    assert store.frozen == []


async def test_freezing_a_draft_returns_the_frozen_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: freezing a draft stores it under the revision the author saw."""
    store = _Store()
    store.submissions[SUBMISSION_ID] = {
        "id": SUBMISSION_ID,
        "submitted_by": MEMBER,
        "status": "draft",
        "revision": 3,
    }
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/marketplace/submissions/{SUBMISSION_ID}/submit",
        principal=_principal(),
        json_body={"expected_revision": 3},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "submitted"
    assert store.frozen == [{"expected_revision": 3}]


async def test_install_rebinds_a_declared_knowledge_base_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a declared slot must resolve to a tenant object, and the workflow the
    tenant receives carries the bound id instead of the placeholder."""
    version = placeholder_version()
    workflows = _Workflows()

    # An unbound declared slot cannot install, and nothing is persisted.
    store = _Store(published=version)
    app = _app(store, monkeypatch, bindings=_Bindings(), workflows=workflows)
    unbound = await _request(
        app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body=_install_body(version),
    )
    assert unbound.status_code == 400, unbound.text
    assert unbound.json()["error"]["code"] == ErrorCode.WORKBUDDY_REBINDING_INCOMPLETE.value
    assert store.installations == {}
    assert workflows.created == []

    # A binding that names no knowledge base of this tenant is refused the same way.
    foreign_store = _Store(published=version)
    foreign_app = _app(foreign_store, monkeypatch, bindings=_Bindings(), workflows=workflows)
    foreign = await _request(
        foreign_app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body=_install_body(version, bindings={"knowledge_bases": {KB_SLOT: KB_ID}}),
    )
    assert foreign.status_code == 400, foreign.text
    assert foreign.json()["error"]["code"] == ErrorCode.WORKBUDDY_REBINDING_INCOMPLETE.value
    assert foreign_store.installations == {}

    # The slot resolves: the tenant's installed workflow carries the bound id.
    bound_store = _Store(published=version)
    bound_app = _app(
        bound_store, monkeypatch, bindings=_Bindings(knowledge_bases={KB_ID}), workflows=workflows
    )
    installed = await _request(
        bound_app,
        "POST",
        f"/marketplace/templates/{TEMPLATE}/install",
        principal=_principal(),
        json_body=_install_body(version, bindings={"knowledge_bases": {KB_SLOT: KB_ID}}),
    )
    assert installed.status_code == 202, installed.text
    assert installed.json()["data"]["installation"]["id"] == INSTALLATION_ID
    definition = workflows.created[0]["definition"]
    assert definition["nodes"][0]["config"]["input"]["kb"] == KB_ID
    assert PLACEHOLDER not in str(definition)
    assert bound_store.consents[0]["capabilities_hash"] == M.sha256_hex(
        M.canonical_json(list(version["required_capabilities"]))
    )


async def test_upgrade_accepts_the_console_body_and_keeps_its_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: the console sends ``template_id``; a mismatch is refused, not ignored."""
    version = published_version()
    installation = {
        "id": INSTALLATION_ID,
        "tenant_id": TENANT,
        "template_id": TEMPLATE,
        "template_version_id": WORKFLOW_VERSION_ID,
        "workflow_id": WORKFLOW_ID,
        "installed_version_id": WORKFLOW_VERSION_ID,
        "installed_by": MEMBER,
        "status": "installed",
        "revision": 2,
        "job_id": JOB_ID,
    }
    store = _Store(published=version)
    store.installations[INSTALLATION_ID] = installation
    workflows = _Workflows()
    app = _app(store, monkeypatch, bindings=_Bindings(), workflows=workflows)
    monkeypatch.setattr(
        api, "WorkBuddyMarketplaceRepo", lambda _db: _InstallationLookup(installation)
    )
    body = {key: value for key, value in _install_body(version).items() if key != "workflow_name"}
    body["template_id"] = TEMPLATE

    upgraded = await _request(
        app,
        "POST",
        f"/marketplace/installations/{INSTALLATION_ID}/upgrade",
        principal=_principal(),
        json_body=body,
    )
    assert upgraded.status_code == 202, upgraded.text
    assert upgraded.json()["data"]["upgrade"]["to_template_version_id"] == TEMPLATE_VERSION
    assert workflows.created[0]["workflow_id"] == WORKFLOW_ID

    mismatched = await _request(
        app,
        "POST",
        f"/marketplace/installations/{INSTALLATION_ID}/upgrade",
        principal=_principal(),
        json_body={**body, "template_id": KB_ID},
    )
    assert mismatched.status_code == 400, mismatched.text
    assert mismatched.json()["error"]["code"] == ErrorCode.WORKBUDDY_INVALID_ARGUMENT.value
    assert len(workflows.created) == 1


async def test_decision_returns_the_submission_under_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: the console reads ``result.submission``; the row is not flattened."""
    store = _Store()
    store.submissions[SUBMISSION_ID] = {
        "id": SUBMISSION_ID,
        "tenant_id": TENANT,
        "submitted_by": MEMBER,
        "name": "Greeting template",
        "industry": "general",
        "summary": "Greets the caller.",
        "license_id": "octop-community",
        "license_text_hash": M.sha256_hex(PUBLISHER_LICENSE),
        "requested_capabilities": [],
        "status": "submitted",
        "revision": 4,
        "frozen_definition": template_definition(),
        "frozen_definition_hash": M.definition_sha256(template_definition()),
    }
    app = _app(store, monkeypatch)
    platform_dependency = api._Platform.__metadata__[0].dependency  # type: ignore[attr-defined]
    app.dependency_overrides[platform_dependency] = lambda: WorkBuddyPlatformPrincipal(
        user=User(id=99, username="reviewer", role=Role.USER, display_name=None),
        audience="workbuddy-platform",
        claims={},
    )

    response = await _request(
        app,
        "POST",
        f"/platform/submissions/{TENANT}/{SUBMISSION_ID}/decisions",
        principal=_principal(),
        json_body={
            "decision": "approved",
            "platform_review_ref": "review-1",
            "expected_revision": 4,
            "publication": {"version": "1.0.0", "publisher_display": "Octop Labs"},
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["submission"]["id"] == SUBMISSION_ID
    assert data["submission"]["status"] == "approved"
    assert data["submission"]["published_template_version_id"] == TEMPLATE_VERSION
    assert data["review"]["decision"] == "approved"
    assert data["published_version"]["template_version_id"] == TEMPLATE_VERSION


async def test_platform_decisions_need_the_platform_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T24: a tenant member cannot decide another tenant's submission."""
    store = _Store()
    app = _app(store, monkeypatch)
    response = await _request(
        app,
        "POST",
        f"/platform/submissions/{TENANT}/{SUBMISSION_ID}/decisions",
        principal=_principal(role="admin"),
        json_body={"decision": "approved", "platform_review_ref": "review-1"},
    )
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == ErrorCode.AUTH_FAILED.value

"""Focused unit tests for the WorkBuddy catalog API router (no live PostgreSQL).

Every test drives the router functions (or a minimal HTTP app) with a fake
catalog repo and a fake secret store, so they prove secret handling and
authorization without a database: the raw secret may only reach the secret
store, catalog metadata may only carry a server-generated reference, and
cross-tenant or not-owned objects stay invisible as 404.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from octop.api.deps import current_user, get_server
from octop.api.routers import workbuddy_catalog as catalog
from octop.api.routers.workbuddy_identity import WorkBuddyPrincipal, workbuddy_principal
from octop.infra.connectors.crypto import decrypt_credentials
from octop.infra.db.repos.workbuddy_catalog import (
    WorkBuddyCapabilities,
    WorkBuddyCasConflict,
    WorkBuddyCredential,
    WorkBuddyCredentialNameTaken,
    WorkBuddyGrant,
    WorkBuddyModelRevision,
    WorkBuddyPlatformRevisionConflict,
    WorkBuddyRevisionRevoked,
    WorkBuddyToolRevision,
)
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role, User

TENANT = "11111111-1111-4111-8111-111111111111"
OWNER_MEMBER = "22222222-2222-4222-8222-222222222222"
OTHER_MEMBER = "33333333-3333-4333-8333-333333333333"
CREDENTIAL_ID = "44444444-4444-4444-8444-444444444444"
TOOL_REVISION_ID = "55555555-5555-4555-8555-555555555555"
MODEL_REVISION_ID = "66666666-6666-4666-8666-666666666666"
GRANT_ID = "77777777-7777-4777-8777-777777777777"

_SECRET = "sk-live-do-not-leak"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _FakeSecretStore:
    """In-memory stand-in for Octop ``SecretRepo``."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    def get_or_create(self, key: str, factory: Any) -> bytes:
        if key not in self.values:
            self.values[key] = factory()
        return self.values[key]

    def credential_values(self) -> list[bytes]:
        return [v for k, v in self.values.items() if k.startswith("workbuddy.credential.")]


def _credential(
    *,
    credential_id: str = CREDENTIAL_ID,
    tenant_id: str = TENANT,
    name: str = "github",
    status: str = "active",
    revision: int = 1,
    external_ref: str = "octop-secret://workbuddy.credential.seed.r1.deadbeef",
    owner_member_id: str = OWNER_MEMBER,
) -> WorkBuddyCredential:
    return WorkBuddyCredential(
        credential_id=credential_id,
        tenant_id=tenant_id,
        name=name,
        connector_kind="github",
        description="ci credential",
        scopes=("issues:read",),
        status=status,
        revision=revision,
        external_ref=external_ref,
        owner_member_id=owner_member_id,
        created_at=1_700_000_000,
        updated_at=1_700_000_500,
        revoked_at=1_700_000_900 if status == "revoked" else None,
        revoked_by_member_id=OWNER_MEMBER if status == "revoked" else None,
    )


def _capabilities(
    *,
    tool_ids: tuple[str, ...] = (),
    model_ids: tuple[str, ...] = (),
    default_model_id: str | None = None,
    revision: int = 1,
) -> WorkBuddyCapabilities:
    return WorkBuddyCapabilities(
        tenant_id=TENANT,
        revision=revision,
        tool_revision_ids=tool_ids,
        model_revision_ids=model_ids,
        default_model_revision_id=default_model_id,
        updated_at=1_700_000_000,
        updated_by_member_id=OWNER_MEMBER,
    )


def _tool_revision(
    *,
    status: str = "published",
    effect_class: str = "external_write",
    supports_idempotency: bool = False,
    supports_result_lookup: bool = False,
    sandbox_verified: bool = False,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> WorkBuddyToolRevision:
    return WorkBuddyToolRevision(
        tool_revision_id=TOOL_REVISION_ID,
        adapter_key="builtin",
        tool_key="web_search",
        revision=1,
        display_name="Web Search",
        description="",
        status=status,
        published_by_user_id=1,
        published_at=1_700_000_000,
        revoked_by_user_id=None,
        revoked_at=None,
        effect_class=effect_class,
        supports_idempotency=supports_idempotency,
        supports_result_lookup=supports_result_lookup,
        sandbox_verified=sandbox_verified,
        input_schema=input_schema,
        output_schema=output_schema,
    )


def _model_revision(*, status: str = "published") -> WorkBuddyModelRevision:
    return WorkBuddyModelRevision(
        model_revision_id=MODEL_REVISION_ID,
        adapter_key="openai",
        model_key="gpt-4o-mini",
        revision=1,
        display_name="GPT-4o mini",
        description="",
        status=status,
        published_by_user_id=1,
        published_at=1_700_000_000,
        revoked_by_user_id=None,
        revoked_at=None,
    )


class _FakeCatalogRepo:
    """In-memory catalog repo recording every call the router makes."""

    def __init__(self, *, credentials: list[WorkBuddyCredential] | None = None) -> None:
        self.credentials = {c.credential_id: c for c in (credentials or [_credential()])}
        self.grants: list[WorkBuddyGrant] = []
        self.members: set[str] = {OWNER_MEMBER, OTHER_MEMBER}
        self.public_tools: list[WorkBuddyToolRevision] = []
        self.public_models: list[WorkBuddyModelRevision] = []
        self.capabilities: WorkBuddyCapabilities | None = None
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.tool_failure: Exception | None = None
        self.model_failure: Exception | None = None
        self.capability_failure: Exception | None = None
        self.credential_failure: Exception | None = None
        self.rotate_conflict: Exception | None = None

    # -- credentials ------------------------------------------------------ #

    def list_credentials(self, tenant_id: str) -> list[WorkBuddyCredential]:
        self.calls.append(("list_credentials", (tenant_id,), {}))
        return [c for c in self.credentials.values() if c.tenant_id == tenant_id]

    def get_credential(self, tenant_id: str, credential_id: str) -> WorkBuddyCredential | None:
        self.calls.append(("get_credential", (tenant_id, credential_id), {}))
        credential = self.credentials.get(credential_id)
        if credential is None or credential.tenant_id != tenant_id:
            return None
        return credential

    def create_credential(self, tenant_id: str, **kwargs: Any) -> WorkBuddyCredential:
        self.calls.append(("create_credential", (tenant_id,), kwargs))
        if self.credential_failure is not None:
            raise self.credential_failure
        if any(
            c.name == kwargs["name"] and c.tenant_id == tenant_id for c in self.credentials.values()
        ):
            raise WorkBuddyCredentialNameTaken(
                code="WORKBUDDY_CREDENTIAL_NAME_TAKEN", message="name already used"
            )
        record = _credential(
            credential_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            name=kwargs["name"],
            external_ref=kwargs["external_ref"],
            owner_member_id=kwargs["actor_member_id"],
        )
        self.credentials[record.credential_id] = record
        return record

    def rotate_credential(
        self, tenant_id: str, credential_id: str, **kwargs: Any
    ) -> WorkBuddyCredential | None:
        self.calls.append(("rotate_credential", (tenant_id, credential_id), kwargs))
        if self.rotate_conflict is not None:
            raise self.rotate_conflict
        current = self.get_credential(tenant_id, credential_id)
        if current is None:
            return None
        rotated = _credential(
            credential_id=current.credential_id,
            tenant_id=current.tenant_id,
            name=current.name,
            status=current.status,
            revision=current.revision + 1,
            external_ref=kwargs["external_ref"],
            owner_member_id=current.owner_member_id,
        )
        self.credentials[credential_id] = rotated
        return rotated

    def revoke_credential(
        self, tenant_id: str, credential_id: str, **kwargs: Any
    ) -> WorkBuddyCredential | None:
        self.calls.append(("revoke_credential", (tenant_id, credential_id), kwargs))
        current = self.get_credential(tenant_id, credential_id)
        if current is None:
            return None
        revoked = _credential(
            credential_id=current.credential_id,
            tenant_id=current.tenant_id,
            name=current.name,
            status="revoked",
            revision=current.revision,
            external_ref=current.external_ref,
            owner_member_id=current.owner_member_id,
        )
        self.credentials[credential_id] = revoked
        return revoked

    # -- grants ----------------------------------------------------------- #

    def list_grants(self, tenant_id: str, credential_id: str) -> list[WorkBuddyGrant]:
        self.calls.append(("list_grants", (tenant_id, credential_id), {}))
        return [g for g in self.grants if g.credential_id == credential_id]

    def create_grant(
        self, tenant_id: str, credential_id: str, **kwargs: Any
    ) -> WorkBuddyGrant | None:
        self.calls.append(("create_grant", (tenant_id, credential_id), kwargs))
        if self.get_credential(tenant_id, credential_id) is None:
            return None
        if kwargs["target_member_id"] not in self.members:
            return None
        grant = WorkBuddyGrant(
            grant_id=GRANT_ID,
            tenant_id=tenant_id,
            credential_id=credential_id,
            member_id=kwargs["target_member_id"],
            granted_by_member_id=kwargs["actor_member_id"],
            created_at=1_700_000_000,
        )
        self.grants.append(grant)
        return grant

    def delete_grant(self, tenant_id: str, credential_id: str, member_id: str) -> bool:
        self.calls.append(("delete_grant", (tenant_id, credential_id, member_id), {}))
        before = len(self.grants)
        self.grants = [
            g
            for g in self.grants
            if not (g.credential_id == credential_id and g.member_id == member_id)
        ]
        return len(self.grants) < before

    # -- platform --------------------------------------------------------- #

    def publish_tool(self, **kwargs: Any) -> WorkBuddyToolRevision:
        self.calls.append(("publish_tool", (), kwargs))
        if self.tool_failure is not None:
            raise self.tool_failure
        declared = {
            name: kwargs[name]
            for name in (
                "effect_class",
                "supports_idempotency",
                "supports_result_lookup",
                "sandbox_verified",
                "input_schema",
                "output_schema",
            )
            if name in kwargs and kwargs[name] is not None
        }
        # The record echoes the declaration, the way the real insert does.
        return _tool_revision(**declared)

    def revoke_tool(self, tool_revision_id: str, **kwargs: Any) -> bool:
        self.calls.append(("revoke_tool", (tool_revision_id,), kwargs))
        if tool_revision_id != TOOL_REVISION_ID:
            return False
        if self.tool_failure is not None:
            raise self.tool_failure
        return True

    def get_tool_revision(self, tool_revision_id: str) -> WorkBuddyToolRevision | None:
        self.calls.append(("get_tool_revision", (tool_revision_id,), {}))
        return _tool_revision(status="revoked") if tool_revision_id == TOOL_REVISION_ID else None

    def publish_model(self, **kwargs: Any) -> WorkBuddyModelRevision:
        self.calls.append(("publish_model", (), kwargs))
        if self.model_failure is not None:
            raise self.model_failure
        return _model_revision()

    def revoke_model(self, model_revision_id: str, **kwargs: Any) -> bool:
        self.calls.append(("revoke_model", (model_revision_id,), kwargs))
        if model_revision_id != MODEL_REVISION_ID:
            return False
        if self.model_failure is not None:
            raise self.model_failure
        return True

    def get_model_revision(self, model_revision_id: str) -> WorkBuddyModelRevision | None:
        self.calls.append(("get_model_revision", (model_revision_id,), {}))
        return _model_revision(status="revoked") if model_revision_id == MODEL_REVISION_ID else None

    def list_public_tools(self) -> list[WorkBuddyToolRevision]:
        self.calls.append(("list_public_tools", (), {}))
        return list(self.public_tools)

    def list_public_models(self) -> list[WorkBuddyModelRevision]:
        self.calls.append(("list_public_models", (), {}))
        return list(self.public_models)

    # -- capabilities ----------------------------------------------------- #

    def get_capabilities(self, tenant_id: str) -> WorkBuddyCapabilities | None:
        self.calls.append(("get_capabilities", (tenant_id,), {}))
        return self.capabilities

    def update_capabilities(self, tenant_id: str, **kwargs: Any) -> WorkBuddyCapabilities:
        self.calls.append(("update_capabilities", (tenant_id,), kwargs))
        if self.capability_failure is not None:
            raise self.capability_failure
        record = _capabilities(
            tool_ids=tuple(kwargs["tool_revision_ids"]),
            model_ids=tuple(kwargs["model_revision_ids"]),
            default_model_id=kwargs["default_model_revision_id"],
            revision=(self.capabilities.revision + 1) if self.capabilities else 1,
        )
        self.capabilities = record
        return record


class _FakeServer:
    def __init__(self, secret_store: _FakeSecretStore | None = None) -> None:
        self.services = type(
            "Services",
            (),
            {"db": object(), "secret_repo": secret_store or _FakeSecretStore()},
        )()


@pytest.fixture
def secret_store() -> _FakeSecretStore:
    return _FakeSecretStore()


@pytest.fixture
def server(secret_store: _FakeSecretStore) -> _FakeServer:
    return _FakeServer(secret_store)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> _FakeCatalogRepo:
    fake = _FakeCatalogRepo()
    monkeypatch.setattr(catalog, "_repo", lambda _server: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_secret_backend_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(catalog._SECRET_BACKEND_ENV, raising=False)
    monkeypatch.delenv(catalog._VAULT_ADDR_ENV, raising=False)
    yield


def _principal(
    *,
    member_id: str = OWNER_MEMBER,
    user_id: int = 7,
    role: str = "admin",
) -> WorkBuddyPrincipal:
    return WorkBuddyPrincipal(
        user=User(id=user_id, username=f"member{user_id}", role=Role.USER, display_name=None),
        tenant_id=TENANT,
        tenant_slug="acme",
        tenant_name="Acme",
        member_id=member_id,
        role=role,
        department_id=None,
        member_status="active",
        tenant_status="active",
    )


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/connector-credentials",
            "headers": [(b"x-request-id", b"req_test123")],
        }
    )


def _assert_envelope(response: dict[str, Any]) -> dict[str, Any]:
    assert set(response) == {"data", "request_id"}
    assert response["request_id"] == "req_test123"
    assert "secret" not in json.dumps(response)
    assert "external_ref" not in json.dumps(response)
    return response["data"]


def _item_calls(
    record: _FakeCatalogRepo, name: str
) -> list[tuple[Any, tuple[Any, ...], dict[str, Any]]]:
    return [call for call in record.calls if call[0] == name]


# --------------------------------------------------------------------------- #
# route coverage
# --------------------------------------------------------------------------- #

_OWNED_PATH_PREFIXES = (
    "/connector-credentials",
    "/platform/tools",
    "/platform/models",
    "/tool-catalog",
    "/model-catalog",
    "/tenant-capabilities",
)


def _normalize_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def test_router_covers_exactly_the_manifest_routes() -> None:
    manifest_path = Path(__file__).resolve().parents[3] / "contracts" / "route-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {
        (route["method"], _normalize_path(route["path"]), route["success_status"])
        for route in manifest["routes"]
        if route["stage"] == "A2" and route["path"].startswith(_OWNED_PATH_PREFIXES)
    }
    declared = {
        (method, _normalize_path(route.path), route.status_code or 200)
        for route in catalog.router.routes
        for method in route.methods & {"GET", "POST", "PUT", "DELETE", "PATCH"}
    }
    assert declared == expected


# --------------------------------------------------------------------------- #
# secrets never cross the repo or the API boundary
# --------------------------------------------------------------------------- #


def test_create_credential_keeps_secret_in_secret_store_only(
    server: Any, repo: _FakeCatalogRepo, secret_store: _FakeSecretStore
) -> None:
    body = catalog.CredentialCreateBody(
        connector_type="github",
        display_name="github-actions",
        secret={"token": _SECRET},
        allowed_scopes=["issues:read"],
    )
    response = _run(
        catalog.create_connector_credential(
            request=_request(), body=body, principal=_principal(), server=server
        )
    )
    data = _assert_envelope(response)
    assert set(data) == {
        "id",
        "connector_type",
        "display_name",
        "description",
        "owner_id",
        "status",
        "revision",
        "allowed_scopes",
        "rotated_at",
        "revoked_at",
        "created_at",
        "updated_at",
    }
    assert data["owner_id"] == OWNER_MEMBER
    assert data["connector_type"] == "github"
    assert data["display_name"] == "github-actions"
    assert data["allowed_scopes"] == ["issues:read"]

    ((_, _, kwargs),) = _item_calls(repo, "create_credential")
    pointer = kwargs["external_ref"]
    assert pointer.startswith("octop-secret://workbuddy.credential.")
    assert _SECRET not in pointer
    assert kwargs["actor_member_id"] == OWNER_MEMBER

    key = pointer.removeprefix("octop-secret://")
    assert set(secret_store.values) == {"connector_fernet", key}
    blob = secret_store.values[key]
    assert _SECRET.encode() not in blob
    assert decrypt_credentials(secret_store, blob) == {"token": _SECRET}


def test_credential_body_cannot_dictate_the_secret_reference() -> None:
    with pytest.raises(ValidationError):
        catalog.CredentialCreateBody.model_validate(
            {
                "connector_type": "github",
                "display_name": "github",
                "secret": {"token": _SECRET},
                "external_ref": "vault://attacker-chosen",
            }
        )


def test_rotate_publishes_a_new_reference_and_never_returns_material(
    server: Any, repo: _FakeCatalogRepo, secret_store: _FakeSecretStore
) -> None:
    response = _run(
        catalog.rotate_connector_credential(
            request=_request(),
            credential_id=CREDENTIAL_ID,
            body=catalog.CredentialRotateBody(secret={"token": "rotated"}),
            principal=_principal(),
            server=server,
        )
    )
    data = _assert_envelope(response)
    assert data["revision"] == 2
    ((_, _, kwargs),) = _item_calls(repo, "rotate_credential")
    assert kwargs["expected_revision"] == 1
    pointer = kwargs["external_ref"]
    assert pointer != "octop-secret://workbuddy.credential.seed.r1.deadbeef"
    key = pointer.removeprefix("octop-secret://")
    assert key.startswith(f"workbuddy.credential.{CREDENTIAL_ID}.r2.")
    assert secret_store.credential_values() == [secret_store.values[key]]
    assert decrypt_credentials(secret_store, secret_store.values[key]) == {"token": "rotated"}


def test_revoked_credential_blocks_rotation_and_grants(
    server: Any, secret_store: _FakeSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    revoked = _FakeCatalogRepo(credentials=[_credential(status="revoked")])
    monkeypatch.setattr(catalog, "_repo", lambda _server: revoked)

    with pytest.raises(OctopError) as rotate_error:
        _run(
            catalog.rotate_connector_credential(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                body=catalog.CredentialRotateBody(secret={"token": "rotated"}),
                principal=_principal(),
                server=server,
            )
        )
    assert rotate_error.value.code == ErrorCode.WORKBUDDY_CREDENTIAL_REVOKED
    assert rotate_error.value.status == 409
    assert secret_store.credential_values() == []

    with pytest.raises(OctopError) as grant_error:
        _run(
            catalog.create_credential_grant(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                body=catalog.GrantCreateBody(user_id=OTHER_MEMBER),
                principal=_principal(),
                server=server,
            )
        )
    assert grant_error.value.code == ErrorCode.WORKBUDDY_CREDENTIAL_REVOKED
    assert [call for call, _, _ in revoked.calls if call == "create_grant"] == []


def test_grant_conveys_use_capability_without_secret_disclosure(
    server: Any, repo: _FakeCatalogRepo
) -> None:
    response = _run(
        catalog.create_credential_grant(
            request=_request(),
            credential_id=CREDENTIAL_ID,
            body=catalog.GrantCreateBody(user_id=OTHER_MEMBER),
            principal=_principal(),
            server=server,
        )
    )
    data = _assert_envelope(response)
    assert set(data) == {"id", "credential_id", "user_id", "granted_by", "created_at"}
    assert data["user_id"] == OTHER_MEMBER
    assert data["granted_by"] == OWNER_MEMBER

    listed = _assert_envelope(
        _run(
            catalog.list_credential_grants(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                principal=_principal(),
                server=server,
            )
        )
    )
    assert [item["user_id"] for item in listed["items"]] == [OTHER_MEMBER]
    _assert_envelope(
        _run(
            catalog.delete_credential_grant(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                user_id=OTHER_MEMBER,
                principal=_principal(),
                server=server,
            )
        )
    )
    assert repo.grants == []


def test_grant_to_an_unknown_or_foreign_member_is_404(server: Any, repo: _FakeCatalogRepo) -> None:
    repo.members = {OWNER_MEMBER}
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.create_credential_grant(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                body=catalog.GrantCreateBody(user_id=OTHER_MEMBER),
                principal=_principal(),
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
    assert repo.grants == []


def test_rotation_conflict_surfaces_the_credential_revision_code(
    server: Any, repo: _FakeCatalogRepo
) -> None:
    repo.rotate_conflict = WorkBuddyCasConflict(
        code="WORKBUDDY_CREDENTIAL_REVISION_CONFLICT", message="stale revision"
    )
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.rotate_connector_credential(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                body=catalog.CredentialRotateBody(secret={"token": "rotated"}, expected_revision=1),
                principal=_principal(),
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.WORKBUDDY_CREDENTIAL_REVISION_CONFLICT
    assert caught.value.status == 409


# --------------------------------------------------------------------------- #
# secret backend fails closed
# --------------------------------------------------------------------------- #


def test_declared_external_secret_backend_fails_closed_without_persisting(
    server: Any,
    repo: _FakeCatalogRepo,
    secret_store: _FakeSecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(catalog._SECRET_BACKEND_ENV, "vault")
    with pytest.raises(OctopError) as caught:
        _create_credential(server)
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert caught.value.status == 503
    assert secret_store.values == {}
    assert [call for call, _, _ in repo.calls if call == "create_credential"] == []


def test_vault_address_alone_fails_closed(
    server: Any, repo: _FakeCatalogRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(catalog._VAULT_ADDR_ENV, "https://vault.internal")
    with pytest.raises(OctopError) as caught:
        _create_credential(server)
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert [call for call, _, _ in repo.calls if call == "create_credential"] == []


def test_missing_local_secret_store_fails_closed(repo: _FakeCatalogRepo) -> None:
    server = _FakeServer()
    server.services.secret_repo = None
    with pytest.raises(OctopError) as caught:
        _create_credential(server)
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert [call for call, _, _ in repo.calls if call == "create_credential"] == []


def test_sqlite_control_plane_fails_closed(server: _FakeServer) -> None:
    """The fixture pool is not PostgreSQL, so every route refuses with a controlled 503."""
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.list_connector_credentials(
                request=_request(), principal=_principal(), server=server
            )
        )
    assert caught.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert caught.value.status == 503


# --------------------------------------------------------------------------- #
# authorization and invisibility
# --------------------------------------------------------------------------- #


def test_member_sees_only_own_credentials(server: Any, repo: _FakeCatalogRepo) -> None:
    repo.credentials["foreign"] = _credential(
        credential_id="88888888-8888-4888-8888-888888888888", owner_member_id=OTHER_MEMBER
    )
    data = _assert_envelope(
        _run(
            catalog.list_connector_credentials(
                request=_request(),
                principal=_principal(member_id=OTHER_MEMBER, role="member"),
                server=server,
            )
        )
    )
    assert [item["id"] for item in data["items"]] == ["88888888-8888-4888-8888-888888888888"]

    admin_view = _assert_envelope(
        _run(
            catalog.list_connector_credentials(
                request=_request(), principal=_principal(), server=server
            )
        )
    )
    assert len(admin_view["items"]) == 2


def test_foreign_and_malformed_credentials_are_404(server: Any, repo: _FakeCatalogRepo) -> None:
    outsider = _principal(member_id=OTHER_MEMBER, role="member")
    for call in (
        lambda: catalog.rotate_connector_credential(
            request=_request(),
            credential_id=CREDENTIAL_ID,
            body=catalog.CredentialRotateBody(secret={"token": "x"}),
            principal=outsider,
            server=server,
        ),
        lambda: catalog.list_credential_grants(
            request=_request(), credential_id=CREDENTIAL_ID, principal=outsider, server=server
        ),
        lambda: catalog.revoke_connector_credential(
            request=_request(), credential_id=CREDENTIAL_ID, principal=outsider, server=server
        ),
        lambda: catalog.revoke_connector_credential(
            request=_request(), credential_id="not-a-uuid", principal=_principal(), server=server
        ),
    ):
        with pytest.raises(OctopError) as caught:
            _run(call())
        assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND
        assert caught.value.status == 404


def test_tenant_admin_revokes_but_does_not_rotate_someone_elses_credential(
    server: Any, repo: _FakeCatalogRepo
) -> None:
    admin = _principal(member_id=OTHER_MEMBER, role="admin")
    data = _assert_envelope(
        _run(
            catalog.revoke_connector_credential(
                request=_request(), credential_id=CREDENTIAL_ID, principal=admin, server=server
            )
        )
    )
    assert data["status"] == "revoked"
    ((_, _, kwargs),) = _item_calls(repo, "revoke_credential")
    assert kwargs["actor_member_id"] == OTHER_MEMBER

    with pytest.raises(OctopError) as caught:
        _run(
            catalog.rotate_connector_credential(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                body=catalog.CredentialRotateBody(secret={"token": "x"}),
                principal=admin,
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.RESOURCE_NOT_FOUND


def test_cross_tenant_credential_is_not_visible(
    server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _credential(tenant_id="99999999-9999-4999-8999-999999999999")
    repo = _FakeCatalogRepo(credentials=[foreign])
    monkeypatch.setattr(catalog, "_repo", lambda _server: repo)
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.list_credential_grants(
                request=_request(),
                credential_id=CREDENTIAL_ID,
                principal=_principal(),
                server=server,
            )
        )
    assert caught.value.status == 404


# --------------------------------------------------------------------------- #
# platform catalog requires the platform audience
# --------------------------------------------------------------------------- #


def test_platform_routes_reject_a_tenant_token_without_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _jwt(audience=None)
    response = _run(_platform_request("/platform/tools", token=token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_ROLE"


def test_platform_publish_succeeds_with_explicit_platform_audience(
    repo: _FakeCatalogRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "_repo", lambda _server: repo)
    response = _run(
        _platform_request("/platform/tools", token=_jwt(audience=["workbuddy-platform"]))
    )
    assert response.status_code == 201
    body = response.json()
    assert body["data"]["tool_key"] == "web_search"
    ((_, _, kwargs),) = _item_calls(repo, "publish_tool")
    assert kwargs["actor_user_id"] == 7


def test_platform_publish_carries_the_tool_declaration(
    repo: _FakeCatalogRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry body is what the engine later reads, so the API must pass it."""
    monkeypatch.setattr(catalog, "_repo", lambda _server: repo)
    response = _run(
        _platform_request(
            "/platform/tools",
            token=_jwt(audience=["workbuddy-platform"]),
            body={
                "tool_key": "web_search",
                "adapter_key": "builtin",
                "display_name": "Web Search",
                "effect_class": "read_only",
                "supports_idempotency": True,
                "sandbox_verified": True,
                "output_schema": {"type": "object", "required": ["hits"]},
            },
        )
    )
    assert response.status_code == 201, response.text
    ((_, _, kwargs),) = _item_calls(repo, "publish_tool")
    assert kwargs["effect_class"] == "read_only", kwargs
    assert kwargs["supports_idempotency"] is True, kwargs
    assert kwargs["sandbox_verified"] is True, kwargs
    assert kwargs["output_schema"] == {"type": "object", "required": ["hits"]}, kwargs
    body = response.json()["data"]
    assert body["effect_class"] == "read_only", body
    assert body["supports_idempotency"] is True, body
    assert body["sandbox_verified"] is True, body
    assert body["output_schema"] == {"type": "object", "required": ["hits"]}, body


def test_platform_publish_rejects_an_unknown_effect_class(
    repo: _FakeCatalogRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog, "_repo", lambda _server: repo)
    response = _run(
        _platform_request(
            "/platform/tools",
            token=_jwt(audience=["workbuddy-platform"]),
            body={
                "tool_key": "web_search",
                "adapter_key": "builtin",
                "display_name": "Web Search",
                "effect_class": "side_effect",
            },
        )
    )
    assert response.status_code == 422, response.text


def test_platform_revoke_requires_platform_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _run(
        _platform_request(
            f"/platform/models/{MODEL_REVISION_ID}/revoke", token=_jwt(audience=["octop"])
        )
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# platform and capability error codes
# --------------------------------------------------------------------------- #


def test_platform_publish_conflicts_surface_stable_codes(
    server: Any, repo: _FakeCatalogRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.tool_failure = WorkBuddyPlatformRevisionConflict(
        code="WORKBUDDY_PLATFORM_REVISION_CONFLICT", message="revision already live"
    )
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.publish_platform_tool(
                request=_request(),
                body=catalog.ToolPublishBody(
                    tool_key="web_search", adapter_key="builtin", display_name="Web Search"
                ),
                platform=_principal(),
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.WORKBUDDY_PLATFORM_REVISION_CONFLICT
    assert caught.value.status == 409

    repo.tool_failure = WorkBuddyRevisionRevoked(
        code="WORKBUDDY_PLATFORM_REVISION_REVOKED", message="already revoked"
    )
    with pytest.raises(OctopError) as caught_revoke:
        _run(
            catalog.revoke_platform_tool(
                request=_request(), tool_id=TOOL_REVISION_ID, platform=_principal(), server=server
            )
        )
    assert caught_revoke.value.code == ErrorCode.WORKBUDDY_PLATFORM_REVISION_REVOKED
    assert caught_revoke.value.status == 409

    with pytest.raises(OctopError) as missing:
        _run(
            catalog.revoke_platform_model(
                request=_request(),
                model_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                platform=_principal(),
                server=server,
            )
        )
    assert missing.value.status == 404


def test_capability_update_replaces_allowances_and_refuses_unapproved(
    server: Any, repo: _FakeCatalogRepo
) -> None:
    data = _assert_envelope(
        _run(
            catalog.update_tenant_capabilities(
                request=_request(),
                body=catalog.CapabilitiesBody(
                    tool_ids=[TOOL_REVISION_ID, TOOL_REVISION_ID],
                    model_ids=[MODEL_REVISION_ID],
                    default_model_id=MODEL_REVISION_ID,
                ),
                admin=_principal(),
                server=server,
            )
        )
    )
    assert data["tool_ids"] == [TOOL_REVISION_ID]
    assert data["model_ids"] == [MODEL_REVISION_ID]
    assert data["default_model_id"] == MODEL_REVISION_ID
    assert data["revision"] == 1
    ((_, _, kwargs),) = _item_calls(repo, "update_capabilities")
    assert kwargs["tool_revision_ids"] == (TOOL_REVISION_ID,)
    assert kwargs["actor_member_id"] == OWNER_MEMBER

    repo.capability_failure = WorkBuddyRevisionRevoked(
        code="FORBIDDEN_ROLE", message="revision is not approved"
    )
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.update_tenant_capabilities(
                request=_request(),
                body=catalog.CapabilitiesBody(tool_ids=[TOOL_REVISION_ID]),
                admin=_principal(),
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.FORBIDDEN_ROLE
    assert caught.value.status == 403

    repo.capability_failure = WorkBuddyCasConflict(
        code="WORKBUDDY_CAPABILITY_REVISION_CONFLICT", message="stale revision"
    )
    with pytest.raises(OctopError) as conflict:
        _run(
            catalog.update_tenant_capabilities(
                request=_request(),
                body=catalog.CapabilitiesBody(model_ids=[MODEL_REVISION_ID], expected_revision=1),
                admin=_principal(),
                server=server,
            )
        )
    assert conflict.value.code == ErrorCode.WORKBUDDY_CAPABILITY_REVISION_CONFLICT
    assert conflict.value.status == 409


def test_capability_default_must_be_an_approved_model(server: Any, repo: _FakeCatalogRepo) -> None:
    with pytest.raises(OctopError) as caught:
        _run(
            catalog.update_tenant_capabilities(
                request=_request(),
                body=catalog.CapabilitiesBody(model_ids=[], default_model_id=MODEL_REVISION_ID),
                admin=_principal(),
                server=server,
            )
        )
    assert caught.value.code == ErrorCode.FORBIDDEN_ROLE
    assert [call for call, _, _ in repo.calls if call == "update_capabilities"] == []


# --------------------------------------------------------------------------- #
# tenant catalogs expose only approved published revisions
# --------------------------------------------------------------------------- #


def test_catalogs_return_only_tenant_approved_published_revisions(
    server: Any, repo: _FakeCatalogRepo
) -> None:
    repo.public_tools = [_tool_revision()]
    repo.public_models = [_model_revision()]
    repo.capabilities = _capabilities(tool_ids=(TOOL_REVISION_ID,), model_ids=(MODEL_REVISION_ID,))
    tools = _assert_envelope(
        _run(catalog.read_tool_catalog(request=_request(), principal=_principal(), server=server))
    )
    models = _assert_envelope(
        _run(catalog.read_model_catalog(request=_request(), principal=_principal(), server=server))
    )
    assert [item["id"] for item in tools["items"]] == [TOOL_REVISION_ID]
    assert [item["id"] for item in models["items"]] == [MODEL_REVISION_ID]

    repo.capabilities = _capabilities()
    empty = _assert_envelope(
        _run(catalog.read_tool_catalog(request=_request(), principal=_principal(), server=server))
    )
    assert empty["items"] == []
    assert _assert_envelope(
        _run(
            catalog.read_tenant_capabilities(request=_request(), admin=_principal(), server=server)
        )
    ) == {
        "tenant_id": TENANT,
        "tool_ids": [],
        "model_ids": [],
        "default_model_id": None,
        "revision": 1,
        "updated_at": 1_700_000_000,
    }


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _run(value: Any) -> Any:
    """Run a router coroutine to completion from a sync test."""
    import asyncio

    return asyncio.run(value)


def _create_credential(server: Any) -> Any:
    return _run(
        catalog.create_connector_credential(
            request=_request(),
            body=catalog.CredentialCreateBody(
                connector_type="github", display_name="github-actions", secret={"token": _SECRET}
            ),
            principal=_principal(),
            server=server,
        )
    )


def _jwt(*, audience: list[str] | None) -> str:
    payload: dict[str, Any] = {
        "sub": "7",
        "uname": "member7",
        "role": "user",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    if audience is not None:
        payload["aud"] = audience
    return jwt.encode(payload, b"workbuddy-test-secret-0123456789abcdef", algorithm="HS256")


def _platform_request(path: str, *, token: str, body: dict[str, Any] | None = None) -> Any:
    """POST ``/api/v1{path}`` through a minimal app with the real audience guard."""
    from unittest.mock import MagicMock

    server = MagicMock()
    server.services.secret_repo.get.return_value = b"workbuddy-test-secret-0123456789abcdef"
    server.services.db = object()
    server.user_manager.get_by_id.return_value = User(
        id=7, username="member7", role=Role.USER, display_name=None
    )
    app = FastAPI()
    app.include_router(catalog.router, prefix="/api/v1")
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[current_user] = lambda: User(
        id=7, username="member7", role=Role.USER, display_name=None
    )
    app.dependency_overrides[workbuddy_principal] = lambda: _principal()

    @app.exception_handler(OctopError)
    async def _octop_error(_request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    payload = (
        json.dumps(body)
        if body is not None
        else (
            '{"tool_key": "web_search", "adapter_key": "builtin", "display_name": "Web Search"}'
            if path == "/platform/tools"
            else "{}"
        )
    )

    async def _send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                f"/api/v1{path}",
                content=payload,
                headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            )

    return _send()

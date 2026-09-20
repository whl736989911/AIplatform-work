"""Unit tests for the WorkBuddy tenant identity / governance API.

No live PostgreSQL is required: the router reaches the identity slice through
the ``_identity_repo`` module seam, which these tests replace with an in-memory
store returning the same row dicts as ``WorkBuddyIdentityRepo`` (tenant-scoped
misses return ``None``, domain rejections raise ``ValueError`` with a stable
``.code``), so the API's 404/409/410 mapping is exercised for real.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from tests.support.app import ensure_control_plane_bound

from octop.api.deps import decode_token, is_jwt_exempt_path, sign_token
from octop.api.middleware.jwt_auth import install as install_jwt_auth
from octop.api.routers import workbuddy_identity as wb_id
from octop.infra.db.repos._base import UNSET
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import Role, User

TEST_PASSWORD = "TestPass12"
NOW = int(time.time())
BASE_HEADERS = {"X-Tenant-Slug": "acme"}


class FakeStoreError(ValueError):
    """Same shape as the identity slice's ``WorkBuddyError``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# In-memory stand-in for the PostgreSQL identity slice
# --------------------------------------------------------------------------- #


class FakeIdentityRepo:
    """Tenant-scoped in-memory implementation of the identity repo surface."""

    def __init__(self, account: Callable[[int], Any]) -> None:
        self.account = account
        self.tenants: dict[str, dict[str, Any]] = {}
        self.members: list[dict[str, Any]] = []
        self.departments: list[dict[str, Any]] = []
        self.invitations: list[dict[str, Any]] = []
        self.quotas: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[str] = []

    # ----- helpers -----

    def member_for(self, user_id: int, tenant_id: str | None = None) -> dict[str, Any] | None:
        for member in self.members:
            if member["user_id"] == user_id and (
                tenant_id is None or member["tenant_id"] == tenant_id
            ):
                return member
        return None

    def quotas_for(self, tenant_id: str) -> dict[str, int]:
        return {row["metric"]: row["limit"] for row in self.quotas.get(tenant_id, [])}

    def _tenant_row(self, tenant_id: str) -> dict[str, Any] | None:
        return self.tenants.get(tenant_id)

    def _member_row(self, tenant_id: str, member_id: str) -> dict[str, Any] | None:
        for member in self.members:
            if member["tenant_id"] == tenant_id and member["membership_id"] == member_id:
                return member
        return None

    def _scoped(self, ctx_tenant: str, member: Mapping[str, Any]) -> dict[str, Any]:
        tenant = self.tenants[ctx_tenant]
        return {
            "tenant_id": member["tenant_id"],
            "tenant_slug": tenant["slug"],
            "tenant_name": tenant["name"],
            "tenant_status": tenant["status"],
            "id": member["membership_id"],
            "membership_id": member["membership_id"],
            "user_id": member["user_id"],
            "username": member["username"],
            "email": member["email"],
            "user_display_name": member["user_display_name"],
            "display_name": member["display_name"],
            "role": member["role"],
            "department_id": member["department_id"],
            "department_name": member["department_name"],
            "status": member["status"],
            "invited_by": member["invited_by"],
            "joined_at": member["joined_at"],
            "updated_at": member["updated_at"],
            "disabled": member["disabled"],
        }

    # ----- tenants -----

    def get_tenant_by_slug(self, slug: Any) -> dict[str, Any] | None:
        self.calls.append("get_tenant_by_slug")
        text = str(slug or "").strip().lower()
        if not text:
            return None
        return next((t for t in self.tenants.values() if t["slug"] == text), None)

    def get_tenant(self, tenant_id: Any) -> dict[str, Any] | None:
        self.calls.append("get_tenant")
        return self._tenant_row(str(tenant_id))

    def create_tenant(
        self,
        slug: Any,
        name: Any,
        *,
        plan: Any = "standard",
        data_region: Any = "cn",
        owner_user_id: Any = None,
        owner_role: str = "owner",
        created_by: Any = None,
    ) -> dict[str, Any]:
        self.calls.append("create_tenant")
        if any(t["slug"] == slug for t in self.tenants.values()):
            raise FakeStoreError(ErrorCode.WORKBUDDY_TENANT_SLUG_TAKEN, "slug already exists")
        tenant_id = str(uuid4())
        tenant = {
            "id": tenant_id,
            "tenant_id": tenant_id,
            "slug": slug,
            "name": name,
            "status": "active",
            "plan": plan,
            "data_region": data_region,
            "status_reason": None,
            "status_changed_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        self.tenants[tenant_id] = tenant
        self.quotas[tenant_id] = [
            {"metric": "users", "limit": 10, "hard_cap": 100, "used": 0, "unit": "seats"},
            {"metric": "departments", "limit": 5, "hard_cap": 50, "used": 0, "unit": "count"},
        ]
        if owner_user_id is not None:
            self._append_member(tenant_id, int(owner_user_id), role=owner_role)
        return tenant

    def suspend_tenant(
        self, tenant_id: Any, *, reason: Any = None, actor_user_id: Any = None
    ) -> dict[str, Any] | None:
        self.calls.append("suspend_tenant")
        return self._set_status(str(tenant_id), "suspended", reason, actor_user_id)

    def restore_tenant(
        self, tenant_id: Any, *, reason: Any = None, actor_user_id: Any = None
    ) -> dict[str, Any] | None:
        self.calls.append("restore_tenant")
        return self._set_status(str(tenant_id), "active", reason, actor_user_id)

    def _set_status(
        self, tenant_id: str, status: str, reason: Any, actor_user_id: Any
    ) -> dict[str, Any] | None:
        tenant = self._tenant_row(tenant_id)
        if tenant is None:
            return None
        tenant["status"] = status
        tenant["status_reason"] = reason
        tenant["status_changed_at"] = int(time.time())
        for member in self.members:
            if member["tenant_id"] == tenant_id:
                member["status"] = "suspended" if status == "suspended" else "active"
        return tenant

    # ----- memberships -----

    def membership_for_user(
        self, octop_user_id: Any, *, active_only: bool = True
    ) -> dict[str, Any] | None:
        self.calls.append("membership_for_user")
        for member in self.members:
            if member["user_id"] != int(octop_user_id):
                continue
            if active_only and member["status"] != "active":
                continue
            return self._scoped(member["tenant_id"], member)
        return None

    def _append_member(
        self,
        tenant_id: str,
        user_id: int,
        *,
        role: str = "member",
        department_id: str | None = None,
        status: str = "active",
        display_name: str | None = None,
        invitation_token: str | None = None,
        invited_by: int | None = None,
    ) -> dict[str, Any]:
        account = self.account(user_id)
        member = {
            "tenant_id": tenant_id,
            "membership_id": str(uuid4()),
            "user_id": user_id,
            "username": getattr(account, "username", ""),
            "email": getattr(account, "email", None),
            "user_display_name": getattr(account, "display_name", None),
            "display_name": display_name,
            "role": role,
            "department_id": department_id,
            "department_name": None,
            "status": status,
            "invited_by": invited_by,
            "joined_at": int(time.time()),
            "updated_at": int(time.time()),
            "disabled": False,
            "_invitation_token": invitation_token,
        }
        self.members.append(member)
        return member

    def add_membership(
        self,
        tenant_id: Any,
        octop_user_id: Any,
        *,
        role: Any = None,
        department_id: Any = None,
        status: str = "active",
        display_name: Any = None,
        invitation_token: Any = None,
        invited_by: Any = None,
        actor_user_id: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append("add_membership")
        identifier = str(tenant_id)
        if self._tenant_row(identifier) is None:
            return None
        user_id = int(octop_user_id)
        account = self.account(user_id)
        if account is None:
            return None
        if invitation_token is not None:
            invitation = self._find_invitation(str(invitation_token))
            if invitation is None or invitation["tenant_id"] != identifier:
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_INVITATION_INVALID, "invitation is not valid"
                )
            invited = str(invitation["email"]).lower()
            actual = str(getattr(account, "email", "") or "").lower()
            if invited != actual:
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_INVITATION_INVALID, "invitation email does not match"
                )
            state = self._invitation_state(invitation)
            if state == "revoked":
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_INVITATION_REVOKED, "invitation was revoked"
                )
            if state != "pending":
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_INVITATION_ALREADY_ACCEPTED,
                    "invitation was already accepted",
                )
            invitation["accepted_at"] = int(time.time())
            invitation["accepted_by_user_id"] = user_id
            if role is None:
                role = invitation["role"]
            if department_id is None:
                department_id = invitation["department_id"]
        if any(m["tenant_id"] == identifier and m["user_id"] == user_id for m in self.members):
            raise FakeStoreError(ErrorCode.WORKBUDDY_MEMBERSHIP_EXISTS, "already a member")
        return self._append_member(
            identifier,
            user_id,
            role=role or "member",
            department_id=department_id,
            status=status,
            display_name=display_name,
            invitation_token=str(invitation_token) if invitation_token else None,
            invited_by=invited_by,
        )

    def list_members(
        self,
        tenant_id: Any,
        *,
        status: Any = None,
        department_id: Any = None,
        query: Any = None,
        limit: Any = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append("list_members")
        rows = [m for m in self.members if m["tenant_id"] == str(tenant_id)]
        if status is not None:
            rows = [m for m in rows if m["status"] == status]
        if department_id is not None:
            rows = [m for m in rows if m["department_id"] == department_id]
        return [self._scoped(str(tenant_id), m) for m in rows[offset : offset + int(limit)]]

    def get_member(self, tenant_id: Any, member_id: Any) -> dict[str, Any] | None:
        self.calls.append("get_member")
        row = self._member_row(str(tenant_id), str(member_id))
        return self._scoped(str(tenant_id), row) if row else None

    def update_member(
        self,
        tenant_id: Any,
        member_id: Any,
        *,
        role: Any = UNSET,
        department_id: Any = UNSET,
        status: Any = UNSET,
        display_name: Any = UNSET,
        actor_user_id: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append("update_member")
        identifier = str(tenant_id)
        row = self._member_row(identifier, str(member_id))
        if row is None:
            return None
        new_role = row["role"] if role is UNSET else role
        new_status = row["status"] if status is UNSET else status
        if (
            row["role"] == "owner"
            and row["status"] == "active"
            and (new_role != "owner" or new_status != "active")
        ):
            others = [
                m
                for m in self.members
                if m["tenant_id"] == identifier
                and m["role"] == "owner"
                and m["status"] == "active"
                and m["membership_id"] != row["membership_id"]
            ]
            if not others:
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_LAST_OWNER_REQUIRED,
                    "tenant must keep at least one active owner",
                )
        row["role"] = new_role
        row["status"] = new_status
        if department_id is not UNSET:
            row["department_id"] = department_id
        if display_name is not UNSET:
            row["display_name"] = display_name
        row["updated_at"] = int(time.time())
        return self._scoped(identifier, row)

    # ----- departments -----

    def list_departments(
        self,
        tenant_id: Any,
        *,
        status: Any = None,
        parent_id: Any = None,
        limit: Any = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.calls.append("list_departments")
        rows = [d for d in self.departments if d["tenant_id"] == str(tenant_id)]
        return rows[offset : offset + int(limit)]

    def create_department(
        self,
        tenant_id: Any,
        *,
        name: Any,
        parent_id: Any = None,
        description: Any = None,
        manager_user_id: Any = None,
        actor_user_id: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append("create_department")
        identifier = str(tenant_id)
        if self._tenant_row(identifier) is None:
            return None
        row = {
            "id": str(uuid4()),
            "department_id": str(uuid4()),
            "tenant_id": identifier,
            "parent_department_id": parent_id,
            "name": name,
            "description": description,
            "manager_user_id": manager_user_id,
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
        }
        row["id"] = row["department_id"]
        self.departments.append(row)
        return row

    def update_department(
        self,
        tenant_id: Any,
        department_id: Any,
        *,
        name: Any = UNSET,
        description: Any = UNSET,
        parent_id: Any = UNSET,
        manager_user_id: Any = UNSET,
        status: Any = UNSET,
        actor_user_id: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append("update_department")
        identifier = str(tenant_id)
        row = next(
            (
                d
                for d in self.departments
                if d["tenant_id"] == identifier and d["department_id"] == str(department_id)
            ),
            None,
        )
        if row is None:
            return None
        if parent_id is not UNSET and parent_id is not None:
            cursor: Any = parent_id
            seen: set[str] = set()
            while cursor is not None:
                if cursor in seen:
                    break
                seen.add(cursor)
                if cursor == row["department_id"]:
                    raise FakeStoreError(
                        ErrorCode.WORKBUDDY_DEPARTMENT_CYCLE,
                        "department hierarchy would contain a cycle",
                    )
                parent = next((d for d in self.departments if d["department_id"] == cursor), None)
                cursor = parent["parent_department_id"] if parent else None
            row["parent_department_id"] = parent_id
        elif parent_id is None and parent_id is not UNSET:
            row["parent_department_id"] = None
        if name is not UNSET:
            row["name"] = name
        if description is not UNSET:
            row["description"] = description
        if status is not UNSET:
            row["status"] = status
        row["updated_at"] = int(time.time())
        return row

    # ----- invitations -----

    def _find_invitation(self, token: str) -> dict[str, Any] | None:
        digest = hashlib.sha256(str(token).strip().encode("utf-8")).hexdigest()
        return next((i for i in self.invitations if i["token_sha256"] == digest), None)

    def _invitation_state(self, row: Mapping[str, Any]) -> str:
        if row["accepted_at"] is not None:
            return "accepted"
        if row["revoked_at"] is not None:
            return "revoked"
        if int(row["expires_at"]) <= int(time.time()):
            return "expired"
        return "pending"

    def _invitation_dict(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": row["invitation_id"],
            "invitation_id": row["invitation_id"],
            "tenant_id": row["tenant_id"],
            "email": row["email"],
            "role": row["role"],
            "department_id": row["department_id"],
            "status": self._invitation_state(row),
            "invited_by": row["invited_by"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
            "revoked_by": row["revoked_by"],
            "accepted_at": row["accepted_at"],
            "accepted_by_user_id": row["accepted_by_user_id"],
        }

    def create_invitation(
        self,
        tenant_id: Any,
        *,
        email: Any,
        token_sha256: Any,
        expires_at: Any,
        role: str = "member",
        department_id: Any = None,
        invited_by: Any = None,
    ) -> dict[str, Any] | None:
        self.calls.append("create_invitation")
        identifier = str(tenant_id)
        if self._tenant_row(identifier) is None:
            return None
        row = {
            "invitation_id": str(uuid4()),
            "tenant_id": identifier,
            "email": email,
            "role": role,
            "department_id": department_id,
            "token_sha256": token_sha256,
            "invited_by": invited_by,
            "expires_at": expires_at,
            "created_at": int(time.time()),
            "revoked_at": None,
            "revoked_by": None,
            "accepted_at": None,
            "accepted_by_user_id": None,
        }
        self.invitations.append(row)
        return self._invitation_dict(row)

    def list_invitations(
        self, tenant_id: Any, *, status: Any = None, limit: Any = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        self.calls.append("list_invitations")
        rows = [i for i in self.invitations if i["tenant_id"] == str(tenant_id)]
        return [self._invitation_dict(r) for r in rows[offset : offset + int(limit)]]

    def revoke_invitation(
        self, tenant_id: Any, invitation_id: Any, *, revoked_by: Any = None
    ) -> dict[str, Any] | None:
        self.calls.append("revoke_invitation")
        identifier = str(tenant_id)
        row = next(
            (
                i
                for i in self.invitations
                if i["tenant_id"] == identifier and i["invitation_id"] == str(invitation_id)
            ),
            None,
        )
        if row is None:
            return None
        if row["accepted_at"] is not None:
            raise FakeStoreError(
                ErrorCode.WORKBUDDY_INVITATION_ALREADY_ACCEPTED,
                "the invitation has already been accepted",
            )
        if row["revoked_at"] is None:
            row["revoked_at"] = int(time.time())
            row["revoked_by"] = revoked_by
        return self._invitation_dict(row)

    def lookup_invitation(
        self, token: Any, *, require_pending: bool = True, token_is_hash: bool = False
    ) -> dict[str, Any] | None:
        self.calls.append("lookup_invitation")
        if token_is_hash:
            digest = str(token)
        else:
            digest = hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()
        row = next((i for i in self.invitations if i["token_sha256"] == digest), None)
        if row is None:
            return None
        if require_pending and self._invitation_state(row) != "pending":
            return None
        return self._invitation_dict(row)

    # ----- quotas -----

    def get_quotas(self, tenant_id: Any) -> list[dict[str, Any]]:
        self.calls.append("get_quotas")
        return [dict(row) for row in self.quotas.get(str(tenant_id), [])]

    def set_quotas(
        self, tenant_id: Any, quotas: Mapping[str, Any], *, actor_user_id: Any = None
    ) -> list[dict[str, Any]] | None:
        self.calls.append("set_quotas")
        identifier = str(tenant_id)
        if self._tenant_row(identifier) is None:
            return None
        rows = {row["metric"]: row for row in self.quotas.get(identifier, [])}
        for metric, limit in quotas.items():
            if metric not in rows:
                raise FakeStoreError(
                    ErrorCode.WORKBUDDY_QUOTA_METRIC_INVALID, f"unknown quota metric {metric}"
                )
            if int(limit) > int(rows[metric]["hard_cap"]):
                raise FakeStoreError(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"limit for {metric} exceeds the platform hard cap",
                )
            rows[metric]["limit"] = int(limit)
        return [dict(row) for row in self.quotas.get(identifier, [])]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def _token(srv: OctopServer, user: User, *, audience: str | None = None) -> str:
    assert srv.services is not None
    secret = srv.services.secret_repo.get("jwt")
    assert secret is not None
    extra = {"aud": audience} if audience else None
    return sign_token(
        secret,
        sub=user.id,
        uname=user.username,
        role=user.role.value,
        ttl_seconds=3600,
        extra_claims=extra,
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _test_app(srv: OctopServer) -> FastAPI:
    """Minimal app: real JWT middleware + the WorkBuddy identity router."""
    app = FastAPI()
    app.state.octop_server = srv
    install_jwt_auth(app, srv)

    @app.exception_handler(OctopError)
    async def _octop_handler(request: Request, exc: OctopError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    app.include_router(wb_id.router, prefix="/api/v1")
    return app


async def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    srv = OctopServer(home=tmp_path)
    await srv.start()
    await ensure_control_plane_bound(srv)
    assert srv.services is not None
    assert srv.user_manager is not None

    repo = FakeIdentityRepo(account=lambda uid: srv.user_manager.get_row(uid))
    monkeypatch.setattr(wb_id, "_identity_repo", lambda server: repo)

    async def account(username: str, *, role: Role = Role.USER, email: str) -> User:
        return await srv.user_manager.create(
            username=username, password=TEST_PASSWORD, role=role, email=email
        )

    owner = await account("acme-owner", email="owner@acme.test")
    member = await account("acme-member", email="member@acme.test")
    suspended = await account("acme-suspended", email="suspended@acme.test")
    globex_owner = await account("globex-owner", email="owner@globex.test")
    platform_admin = await account("platform", role=Role.ADMIN, email="platform@octop.test")
    stranger = await account("stranger", email="stranger@nowhere.test")

    acme = repo.create_tenant("acme", "Acme Inc", owner_user_id=owner.id)
    globex = repo.create_tenant("globex", "Globex", owner_user_id=globex_owner.id)
    member_row = repo.add_membership(acme["tenant_id"], member.id, role="member")
    suspended_row = repo.add_membership(acme["tenant_id"], suspended.id, role="member")
    suspended_row["status"] = "suspended"
    repo.add_membership(globex["tenant_id"], platform_admin.id, role="member")

    app = _test_app(srv)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    return SimpleNamespace(
        client=client,
        srv=srv,
        repo=repo,
        owner=owner,
        member=member,
        suspended=suspended,
        platform_admin=platform_admin,
        stranger=stranger,
        globex_owner=globex_owner,
        acme=acme,
        globex=globex,
        owner_row=repo.member_for(owner.id, acme["tenant_id"]),
        member_row=member_row,
        suspended_row=suspended_row,
        platform_token=_token(srv, platform_admin, audience=wb_id.WORKBUDDY_PLATFORM_AUDIENCE),
    )


@pytest.fixture
async def wb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    harness = await _seed(tmp_path, monkeypatch)
    try:
        yield harness
    finally:
        await harness.client.aclose()
        await harness.srv.stop()


def _owner_token(wb: Any) -> str:
    return _token(wb.srv, wb.owner)


def _owner_headers(wb: Any) -> dict[str, str]:
    return {**_auth(_owner_token(wb)), **BASE_HEADERS}


def _member_headers(wb: Any) -> dict[str, str]:
    return {**_auth(_token(wb.srv, wb.member)), **BASE_HEADERS}


# --------------------------------------------------------------------------- #
# Route surface
# --------------------------------------------------------------------------- #


def test_routes_are_api_v1_relative_and_match_the_frozen_manifest() -> None:
    actual = {(method, route.path) for route in wb_id.router.routes for method in route.methods}
    assert actual == {
        ("POST", "/auth/login"),
        ("POST", "/auth/reauthenticate"),
        ("POST", "/auth/register"),
        ("GET", "/tenant-context"),
        ("POST", "/tenants"),
        ("GET", "/tenants/{tenant_id}"),
        ("POST", "/tenants/{tenant_id}/suspend"),
        ("POST", "/tenants/{tenant_id}/restore"),
        ("GET", "/users"),
        ("PATCH", "/users/{member_id}"),
        ("GET", "/departments"),
        ("POST", "/departments"),
        ("PATCH", "/departments/{department_id}"),
        ("GET", "/invitations"),
        ("POST", "/invitations"),
        ("POST", "/invitations/{invitation_id}/revoke"),
        ("GET", "/tenant-quotas"),
        ("PUT", "/tenant-quotas"),
    }
    assert all(not route.path.startswith("/api/") for route in wb_id.router.routes)


def test_only_v1_auth_and_webhooks_bypass_jwt() -> None:
    # Preserved legacy Octop behaviour.
    assert is_jwt_exempt_path("/api/auth/login")
    assert not is_jwt_exempt_path("/api/auth/me")
    # WorkBuddy additions.
    assert is_jwt_exempt_path("/api/v1/auth/login")
    assert is_jwt_exempt_path("/api/v1/auth/register")
    assert is_jwt_exempt_path("/api/v1/webhooks/anything")
    # Re-authentication is a step *inside* an authenticated session, never a way
    # in: the password check runs against the caller's own membership.
    assert not is_jwt_exempt_path("/api/v1/auth/reauthenticate")
    assert not is_jwt_exempt_path("/api/v1/users")
    assert not is_jwt_exempt_path("/api/v1/tenants")
    assert not is_jwt_exempt_path("/api/v1/tenant-context")


async def test_sqlite_control_plane_fails_closed(tmp_path: Path) -> None:
    """Without PostgreSQL the real DB seam must refuse WorkBuddy identity work."""
    srv = OctopServer(home=tmp_path)
    await srv.start()
    await ensure_control_plane_bound(srv)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_test_app(srv)), base_url="http://testserver"
        ) as client:
            r = await client.post(
                "/api/v1/auth/login",
                json={"username": "nobody", "password": TEST_PASSWORD},
                headers=BASE_HEADERS,
            )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    finally:
        await srv.stop()


# --------------------------------------------------------------------------- #
# Login / register
# --------------------------------------------------------------------------- #


async def test_login_failures_are_indistinguishable(wb: Any) -> None:
    cases = [
        (
            {"username": "acme-owner", "password": TEST_PASSWORD},
            {"X-Tenant-Slug": "unknown-tenant"},
        ),
        ({"username": "ghost", "password": TEST_PASSWORD}, BASE_HEADERS),
        ({"username": "acme-owner", "password": "WrongPass12"}, BASE_HEADERS),
        ({"username": "acme-owner", "password": TEST_PASSWORD}, {"X-Tenant-Slug": "globex"}),
        ({"username": "stranger", "password": TEST_PASSWORD}, BASE_HEADERS),
        ({"username": "acme-member", "password": TEST_PASSWORD}, {}),
    ]
    observed = []
    for body, headers in cases:
        r = await wb.client.post("/api/v1/auth/login", json=body, headers=headers)
        envelope = r.json()
        assert set(envelope) == {"error"}
        observed.append((r.status_code, envelope["error"]["code"], envelope["error"]["message"]))
    assert observed == [(401, "AUTH_INVALID_CREDENTIALS", "invalid credentials")] * len(cases)


async def test_login_returns_tenant_scoped_token(wb: Any) -> None:
    r = await wb.client.post(
        "/api/v1/auth/login",
        json={"username": "acme-member", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"data", "request_id"}
    data = body["data"]
    assert data["token_type"] == "Bearer"
    assert data["user"]["username"] == "acme-member"
    assert data["user"]["tenant"] == {
        "id": wb.acme["tenant_id"],
        "slug": "acme",
        "name": "Acme Inc",
        "status": "active",
        "role": "member",
    }
    assert wb.srv.services is not None
    secret = wb.srv.services.secret_repo.get("jwt")
    assert secret is not None
    claims = decode_token(secret, data["access_token"])
    assert claims["tnt"] == wb.acme["tenant_id"]
    assert claims["tslug"] == "acme"


async def test_login_denies_suspended_tenant_and_member(wb: Any) -> None:
    wb.acme["status"] = "suspended"
    r = await wb.client.post(
        "/api/v1/auth/login",
        json={"username": "acme-member", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert (r.status_code, r.json()["error"]["code"]) == (401, "AUTH_INVALID_CREDENTIALS")
    wb.acme["status"] = "active"
    wb.suspended_row["status"] = "suspended"
    r = await wb.client.post(
        "/api/v1/auth/login",
        json={"username": "acme-suspended", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert (r.status_code, r.json()["error"]["code"]) == (401, "AUTH_INVALID_CREDENTIALS")


async def test_register_consumes_email_bound_invitation(wb: Any) -> None:
    created = await wb.client.post(
        "/api/v1/invitations",
        json={"email": "newbie@acme.test", "role": "member"},
        headers=_owner_headers(wb),
    )
    assert created.status_code == 201
    raw = created.json()["data"]["invite_token"]
    assert created.headers["cache-control"] == "no-store"

    r = await wb.client.post(
        "/api/v1/auth/register",
        json={"invite_token": raw, "username": "newbie", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert r.status_code == 201
    assert r.headers["cache-control"] == "no-store"
    data = r.json()["data"]
    assert data["user"]["username"] == "newbie"
    assert data["user"]["tenant"]["id"] == wb.acme["tenant_id"]

    created_user = wb.srv.user_manager.get("newbie")
    assert created_user is not None
    member = wb.repo.member_for(created_user.id, wb.acme["tenant_id"])
    assert member is not None and member["role"] == "member"
    assert member["_invitation_token"] == raw
    # Only the digest is persisted — never the raw token.
    stored = next(
        i
        for i in wb.repo.invitations
        if i["token_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    )
    assert stored["accepted_at"] is not None
    assert all(i["token_sha256"] != raw for i in wb.repo.invitations)

    reused = await wb.client.post(
        "/api/v1/auth/register",
        json={"invite_token": raw, "username": "newbie2", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "WORKBUDDY_INVITATION_ALREADY_ACCEPTED"


async def test_register_rejects_invalid_expired_and_foreign_invitations(wb: Any) -> None:
    invalid = await wb.client.post(
        "/api/v1/auth/register",
        json={"invite_token": "not-a-token", "username": "x1", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (
        400,
        "WORKBUDDY_INVITATION_INVALID",
    )
    no_slug = await wb.client.post(
        "/api/v1/auth/register",
        json={"invite_token": "x", "username": "x2", "password": TEST_PASSWORD},
    )
    assert (no_slug.status_code, no_slug.json()["error"]["code"]) == (
        400,
        "WORKBUDDY_INVALID_ARGUMENT",
    )

    created = await wb.client.post(
        "/api/v1/invitations", json={"email": "late@acme.test"}, headers=_owner_headers(wb)
    )
    raw = created.json()["data"]["invite_token"]
    invitation = next(
        i
        for i in wb.repo.invitations
        if i["token_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    )
    invitation["expires_at"] = int(time.time()) - 5
    expired = await wb.client.post(
        "/api/v1/auth/register",
        json={"invite_token": raw, "username": "x3", "password": TEST_PASSWORD},
        headers=BASE_HEADERS,
    )
    assert (expired.status_code, expired.json()["error"]["code"]) == (
        410,
        "WORKBUDDY_INVITATION_EXPIRED",
    )

    other = await wb.client.post(
        "/api/v1/invitations", json={"email": "else@globex.test"}, headers=_owner_headers(wb)
    )
    foreign = await wb.client.post(
        "/api/v1/auth/register",
        json={
            "invite_token": other.json()["data"]["invite_token"],
            "username": "x4",
            "password": TEST_PASSWORD,
        },
        headers={"X-Tenant-Slug": "globex"},
    )
    assert (foreign.status_code, foreign.json()["error"]["code"]) == (
        400,
        "WORKBUDDY_INVITATION_INVALID",
    )


# --------------------------------------------------------------------------- #
# Principal gates
# --------------------------------------------------------------------------- #


async def test_tenant_context_reports_derived_membership(wb: Any) -> None:
    r = await wb.client.get("/api/v1/tenant-context", headers=_member_headers(wb))
    assert r.status_code == 200
    body = r.json()
    assert body["request_id"].startswith("req_")
    assert body["data"] == {
        "tenant": {
            "id": wb.acme["tenant_id"],
            "name": "Acme Inc",
            "slug": "acme",
            "status": "active",
            "plan": "standard",
            "data_region": "cn",
        },
        "membership": {
            "id": wb.member_row["membership_id"],
            "user_id": wb.member.id,
            "role": "member",
            "status": "active",
            "department_id": None,
        },
    }


async def test_forged_tenant_header_cannot_change_context(wb: Any) -> None:
    token = _token(wb.srv, wb.member)
    ok = await wb.client.get(
        "/api/v1/tenant-context", headers={**_auth(token), "X-Tenant-Slug": "acme"}
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["tenant"]["id"] == wb.acme["tenant_id"]

    forged = await wb.client.get(
        "/api/v1/tenant-context", headers={**_auth(token), "X-Tenant-Slug": "globex"}
    )
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "FORBIDDEN_RESOURCE_ACTION"
    assert wb.globex["tenant_id"] not in forged.text

    # A header on its own never selects a tenant: no credentials, no context.
    anonymous = await wb.client.get("/api/v1/tenant-context", headers={"X-Tenant-Slug": "globex"})
    assert anonymous.status_code == 401


async def test_membership_gates_deny_empty_and_suspended_members(wb: Any) -> None:
    r = await wb.client.get("/api/v1/tenant-context", headers=_auth(_token(wb.srv, wb.stranger)))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "FORBIDDEN_ROLE")

    r = await wb.client.get("/api/v1/tenant-context", headers=_auth(_token(wb.srv, wb.suspended)))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "WORKBUDDY_MEMBER_DISABLED")

    wb.acme["status"] = "suspended"
    r = await wb.client.get("/api/v1/users", headers=_auth(_owner_token(wb)))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "TENANT_SUSPENDED")


async def test_octop_admin_is_not_tenant_admin(wb: Any) -> None:
    """An Octop platform admin has no WorkBuddy admin power inside a tenant."""
    assert wb.platform_admin.is_admin
    r = await wb.client.get("/api/v1/users", headers=_auth(_token(wb.srv, wb.platform_admin)))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "FORBIDDEN")


async def test_tenant_members_cannot_reach_admin_routes(wb: Any) -> None:
    headers = _member_headers(wb)
    requests = [
        ("GET", "/api/v1/users", None),
        ("PATCH", f"/api/v1/users/{wb.owner_row['membership_id']}", {"role": "admin"}),
        ("POST", "/api/v1/departments", {"name": "Sneaky"}),
        ("PATCH", f"/api/v1/departments/{uuid4()}", {"name": "Sneaky"}),
        ("GET", "/api/v1/invitations", None),
        ("POST", "/api/v1/invitations", {"email": "sneak@acme.test"}),
        ("GET", "/api/v1/tenant-quotas", None),
        ("PUT", "/api/v1/tenant-quotas", {"quotas": {"users": 1}}),
    ]
    for method, path, body in requests:
        r = await wb.client.request(method, path, json=body, headers=headers)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "FORBIDDEN"), path


# --------------------------------------------------------------------------- #
# Platform administration
# --------------------------------------------------------------------------- #


async def test_platform_routes_require_platform_audience(wb: Any) -> None:
    owner_headers = _owner_headers(wb)
    create_body = {"name": "Initech", "slug": "initech", "owner_email": "owner@acme.test"}
    r = await wb.client.post("/api/v1/tenants", json=create_body, headers=owner_headers)
    assert (r.status_code, r.json()["error"]["code"]) == (
        403,
        "FORBIDDEN_ROLE",
    )
    for method, path in (
        ("POST", f"/api/v1/tenants/{wb.acme['tenant_id']}/suspend"),
        ("POST", f"/api/v1/tenants/{wb.acme['tenant_id']}/restore"),
    ):
        r = await wb.client.request(method, path, headers=owner_headers)
        assert (r.status_code, r.json()["error"]["code"]) == (
            403,
            "FORBIDDEN_ROLE",
        ), path
    # Even an Octop platform admin needs the explicit audience.
    r = await wb.client.post(
        f"/api/v1/tenants/{wb.acme['tenant_id']}/suspend",
        headers=_auth(_token(wb.srv, wb.platform_admin)),
    )
    assert (r.status_code, r.json()["error"]["code"]) == (
        403,
        "FORBIDDEN_ROLE",
    )


async def test_platform_tokens_create_suspend_and_restore_tenants(wb: Any) -> None:
    platform_headers = _auth(wb.platform_token)
    created = await wb.client.post(
        "/api/v1/tenants",
        json={
            "name": "Initech",
            "slug": "initech",
            "owner_email": "member@acme.test",
            "plan": "enterprise",
            "data_region": "cn-north",
        },
        headers=platform_headers,
    )
    assert created.status_code == 201
    body = created.json()["data"]
    assert body["tenant"]["slug"] == "initech"
    assert body["tenant"]["plan"] == "enterprise"
    assert body["tenant"]["data_region"] == "cn-north"
    # The owner account already exists, so it is provisioned directly.
    assert body["admin_invitation"] is None
    owner_member = wb.repo.member_for(wb.member.id, body["tenant"]["id"])
    assert owner_member is not None and owner_member["role"] == "owner"

    invited = await wb.client.post(
        "/api/v1/tenants",
        json={"name": "Umbrella", "slug": "umbrella", "owner_email": "new@umbrella.test"},
        headers=platform_headers,
    )
    assert invited.status_code == 201
    assert invited.headers["cache-control"] == "no-store"
    raw = invited.json()["data"]["admin_invitation"]["invite_token"]
    new_tenant_id = invited.json()["data"]["tenant"]["id"]
    assert wb.repo.member_for(wb.member.id, new_tenant_id) is None
    stored = next(i for i in wb.repo.invitations if i["tenant_id"] == new_tenant_id)
    assert stored["token_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert stored["role"] == "owner"

    suspended = await wb.client.post(
        f"/api/v1/tenants/{wb.acme['tenant_id']}/suspend",
        json={"reason": "billing"},
        headers=platform_headers,
    )
    assert suspended.status_code == 200
    assert suspended.json()["data"]["tenant"]["status"] == "suspended"
    assert wb.acme["status_reason"] == "billing"

    restored = await wb.client.post(
        f"/api/v1/tenants/{wb.acme['tenant_id']}/restore", headers=platform_headers
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["tenant"]["status"] == "active"

    missing = await wb.client.post(f"/api/v1/tenants/{uuid4()}/suspend", headers=platform_headers)
    assert missing.status_code == 404


# --------------------------------------------------------------------------- #
# Users, departments, invitations, quotas
# --------------------------------------------------------------------------- #


async def test_list_and_patch_users(wb: Any) -> None:
    headers = _owner_headers(wb)
    listing = await wb.client.get("/api/v1/users", headers=headers)
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()["data"]}
    assert ids == {
        wb.owner_row["membership_id"],
        wb.member_row["membership_id"],
        wb.suspended_row["membership_id"],
    }
    assert all("password" not in item for item in listing.json()["data"])

    department = await wb.client.post(
        "/api/v1/departments", json={"name": "Engineering"}, headers=headers
    )
    department_id = department.json()["data"]["id"]
    patched = await wb.client.patch(
        f"/api/v1/users/{wb.member_row['membership_id']}",
        json={"role": "admin", "department_id": department_id, "display_name": "Renamed"},
        headers=headers,
    )
    assert patched.status_code == 200
    data = patched.json()["data"]
    assert data["role"] == "admin"
    assert data["department_id"] == department_id
    assert data["display_name"] == "Renamed"

    cleared = await wb.client.patch(
        f"/api/v1/users/{wb.member_row['membership_id']}",
        json={"display_name": None},
        headers=headers,
    )
    assert cleared.json()["data"]["display_name"] is None

    self_change = await wb.client.patch(
        f"/api/v1/users/{wb.owner_row['membership_id']}", json={"role": "member"}, headers=headers
    )
    assert self_change.status_code == 403

    # The identity slice keeps at least one active owner.
    demoted = await wb.client.patch(
        f"/api/v1/users/{wb.owner_row['membership_id']}",
        json={"status": "suspended"},
        headers=headers,
    )
    assert demoted.status_code == 409
    assert demoted.json()["error"]["code"] == "WORKBUDDY_LAST_OWNER_REQUIRED"


async def test_scoped_misses_return_404(wb: Any) -> None:
    headers = _owner_headers(wb)
    foreign_member = wb.repo.member_for(wb.globex_owner.id, wb.globex["tenant_id"])
    assert foreign_member is not None
    cases = [
        ("PATCH", f"/api/v1/users/{foreign_member['membership_id']}", {"role": "admin"}),
        ("PATCH", f"/api/v1/users/{uuid4()}", {"role": "admin"}),
        ("PATCH", f"/api/v1/departments/{uuid4()}", {"name": "ghost"}),
        ("POST", f"/api/v1/invitations/{uuid4()}/revoke", None),
    ]
    for method, path, body in cases:
        r = await wb.client.request(method, path, json=body, headers=headers)
        assert r.status_code == 404, path
        assert r.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_tenant_summary_is_member_safe_and_scoped(wb: Any) -> None:
    member_view = await wb.client.get(
        f"/api/v1/tenants/{wb.acme['tenant_id']}", headers=_member_headers(wb)
    )
    assert member_view.status_code == 200
    assert set(member_view.json()["data"]) == {"tenant", "membership"}

    admin_view = await wb.client.get(
        f"/api/v1/tenants/{wb.acme['tenant_id']}", headers=_owner_headers(wb)
    )
    assert admin_view.status_code == 200
    data = admin_view.json()["data"]
    assert data["member_count"] == 3
    assert data["quotas"]["quotas"]["users"] == 10

    foreign = await wb.client.get(
        f"/api/v1/tenants/{wb.globex['tenant_id']}", headers=_owner_headers(wb)
    )
    assert foreign.status_code == 404
    assert wb.globex["tenant_id"] not in foreign.text


async def test_departments_create_patch_and_reject_cycles(wb: Any) -> None:
    headers = _owner_headers(wb)
    parent = await wb.client.post("/api/v1/departments", json={"name": "Parent"}, headers=headers)
    assert parent.status_code == 201
    parent_id = parent.json()["data"]["id"]
    child = await wb.client.post(
        "/api/v1/departments", json={"name": "Child", "parent_id": parent_id}, headers=headers
    )
    assert child.status_code == 201
    child_id = child.json()["data"]["id"]

    renamed = await wb.client.patch(
        f"/api/v1/departments/{child_id}",
        json={"name": "Renamed", "status": "archived"},
        headers=headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["name"] == "Renamed"
    assert renamed.json()["data"]["status"] == "archived"

    cycle = await wb.client.patch(
        f"/api/v1/departments/{parent_id}", json={"parent_id": child_id}, headers=headers
    )
    assert cycle.status_code == 409
    assert cycle.json()["error"]["code"] == "WORKBUDDY_DEPARTMENT_CYCLE"

    orphan = await wb.client.post(
        "/api/v1/departments", json={"name": "Orphan", "parent_id": str(uuid4())}, headers=headers
    )
    assert orphan.status_code == 404

    listing = await wb.client.get("/api/v1/departments", headers=_member_headers(wb))
    assert listing.status_code == 200
    assert {item["name"] for item in listing.json()["data"]} == {"Parent", "Renamed"}
    assert all("member_count" in item for item in listing.json()["data"])


async def test_invitation_metadata_never_leaks_tokens(wb: Any) -> None:
    headers = _owner_headers(wb)
    created = await wb.client.post(
        "/api/v1/invitations",
        json={"email": "audit@acme.test", "role": "member", "expires_in_hours": 24},
        headers=headers,
    )
    assert created.status_code == 201
    raw = created.json()["data"]["invite_token"]
    assert created.headers["cache-control"] == "no-store"

    listing = await wb.client.get("/api/v1/invitations", headers=headers)
    assert listing.status_code == 200
    assert raw not in listing.text
    assert "invite_token" not in listing.text
    assert "token_sha256" not in listing.text
    for item in listing.json()["data"]:
        assert set(item) == {
            "id",
            "email",
            "role",
            "department_id",
            "status",
            "expires_at",
            "created_at",
            "invited_by",
            "revoked_at",
            "accepted_at",
        }

    revoke = await wb.client.post(
        f"/api/v1/invitations/{created.json()['data']['id']}/revoke", headers=headers
    )
    assert revoke.status_code == 200
    assert revoke.json()["data"]["status"] == "revoked"
    again = await wb.client.post(
        f"/api/v1/invitations/{created.json()['data']['id']}/revoke", headers=headers
    )
    assert again.status_code == 200
    assert again.json()["data"]["status"] == "revoked"


async def test_quota_updates_enforce_platform_hard_caps(wb: Any) -> None:
    headers = _owner_headers(wb)
    read = await wb.client.get("/api/v1/tenant-quotas", headers=headers)
    assert read.status_code == 200
    payload = read.json()["data"]
    assert payload["quotas"]["users"] == 10
    assert payload["hard_caps"]["users"] == 100

    updated = await wb.client.put(
        "/api/v1/tenant-quotas", json={"quotas": {"users": 25}}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["quotas"]["users"] == 25

    wb.repo.calls.clear()
    over = await wb.client.put(
        "/api/v1/tenant-quotas", json={"quotas": {"users": 101}}, headers=headers
    )
    assert over.status_code == 429
    assert over.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert "set_quotas" not in wb.repo.calls
    assert wb.repo.quotas_for(wb.acme["tenant_id"])["users"] == 25

    unknown = await wb.client.put(
        "/api/v1/tenant-quotas", json={"quotas": {"nope": 1}}, headers=headers
    )
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "WORKBUDDY_QUOTA_METRIC_INVALID"

    negative = await wb.client.put(
        "/api/v1/tenant-quotas", json={"quotas": {"users": -1}}, headers=headers
    )
    assert negative.status_code == 400


async def test_envelope_echoes_request_id(wb: Any) -> None:
    headers = {**_owner_headers(wb), "X-Request-ID": "trace-42"}
    r = await wb.client.get("/api/v1/tenant-context", headers=headers)
    assert r.json()["request_id"] == "trace-42"

    generated = await wb.client.get("/api/v1/tenant-context", headers=_owner_headers(wb))
    assert generated.json()["request_id"].startswith("req_")


async def test_resolve_member_user_id_is_tenant_scoped(wb: Any) -> None:
    principal = await wb_id.workbuddy_principal(_request(_owner_headers(wb)), wb.owner, wb.srv)
    assert wb_id.resolve_member_user_id(wb.srv, principal, wb.member_row["membership_id"]) == (
        wb.member.id
    )
    assert wb_id.resolve_member_user_id(wb.srv, principal, str(uuid4())) is None


def _request(headers: Mapping[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/tenant-context",
            "raw_path": b"/api/v1/tenant-context",
            "query_string": b"",
            "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
        }
    )

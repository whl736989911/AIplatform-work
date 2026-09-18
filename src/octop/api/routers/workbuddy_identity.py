"""WorkBuddy tenant identity, governance, and platform-administration API.

Every route is declared ``/api/v1``-relative; the app factory mounts this
router at ``/api/v1``. Tenant identity is always derived from the authenticated
Octop user plus a WorkBuddy membership lookup — ``X-Tenant-Slug`` is only ever
compared with that derived tenant and can never select one.

Persistence comes from the PostgreSQL-only identity slice
(``octop.infra.db.repos.workbuddy_identity``); a SQLite control plane fails
closed with ``WORKBUDDY_POSTGRES_REQUIRED``. That slice raises
:class:`ValueError` subclasses carrying a stable ``.code``; ``_store_errors``
maps those onto the API error envelope so handlers never leak a 500 for a
domain rejection.
"""

from __future__ import annotations

import functools
import hashlib
import re
import secrets
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import jwt
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, extract_raw_token, get_server, sign_token
from octop.infra.db.pool import DatabasePool
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.email import parse_optional_email
from octop.infra.users.identity import Role, User
from octop.infra.users.password import validate_password_policy
from octop.infra.users.permissions import effective_permissions
from octop.infra.utils.locale import normalize_locale, resolve_request_locale
from octop.infra.workbuddy.roles import TENANT_ADMIN_ROLES

router = APIRouter()

# Platform-management tokens are the only way in to POST /tenants and
# /tenants/{id}/suspend|restore. They must carry this explicit audience.
WORKBUDDY_PLATFORM_AUDIENCE = "workbuddy-platform"
# Preference order for the platform signing secret; falls back to the shared
# Octop JWT secret so single-node installs work without extra provisioning.
_PLATFORM_SECRET_KEYS = ("workbuddy_platform_jwt", "jwt")

_TENANT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])$")
_PLAN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_DATA_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")


MEMBER_ROLES = ("owner", "admin", "member")
USER_STATUSES = ("active", "suspended")
DEPARTMENT_STATUSES = ("active", "archived")
INVITATION_DEFAULT_HOURS = 72
INVITATION_MAX_HOURS = 24 * 30

STR_ACTIVE = "active"
STR_SUSPENDED = "suspended"


# --------------------------------------------------------------------------- #
# DB binding, error mapping, row access
# --------------------------------------------------------------------------- #


def _identity_repo(server: Any) -> Any:
    """Build the tenant identity repository, failing closed without PostgreSQL.

    Tests replace this seam with an in-memory store.
    """
    services = getattr(server, "services", None)
    if services is None:
        raise OctopError(
            ErrorCode.SETUP_REQUIRED,
            "control-plane database not configured yet",
            status=503,
        )
    db = getattr(services, "db", None)
    if getattr(db, "dialect", "") != "postgresql":
        raise OctopError(
            ErrorCode.WORKBUDDY_POSTGRES_REQUIRED,
            "WorkBuddy tenant identity requires the PostgreSQL control plane",
        )
    from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo

    # ``dialect`` was checked above, so this is a real PostgreSQL pool.
    assert isinstance(db, DatabasePool), db
    return WorkBuddyIdentityRepo(db)


def _map_store_error(exc: BaseException) -> OctopError:
    """Translate a DB-slice domain rejection (``ValueError`` with ``.code``)."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    text = str(message or exc)
    if isinstance(code, str) and code:
        try:
            return OctopError(ErrorCode(code), text)
        except ValueError:
            pass
    return OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, text)


def _store_errors[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Map identity-slice domain errors onto ``OctopError`` for the envelope."""

    @functools.wraps(fn)
    async def _wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except ValueError as exc:
            if getattr(exc, "code", None):
                raise _map_store_error(exc) from exc
            raise

    return _wrapped


def _attr(row: Any, name: str, default: Any = None) -> Any:
    """Read a field from a DB-slice row dict (or a test double)."""
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


# --------------------------------------------------------------------------- #
# Response envelope and row serialization
# --------------------------------------------------------------------------- #


def workbuddy_envelope(request: Request, data: Any) -> dict[str, Any]:
    """Wrap a WorkBuddy success payload in the frozen ``{data, request_id}`` shape."""
    raw = request.headers.get("x-request-id", "").strip()
    request_id = raw if _REQUEST_ID_RE.fullmatch(raw) else f"req_{uuid4().hex[:12]}"
    return {"data": data, "request_id": request_id}


def _tenant_json(row: Any) -> dict[str, Any]:
    return {
        "id": str(_attr(row, "tenant_id")),
        "slug": _attr(row, "slug"),
        "name": _attr(row, "name"),
        "status": _attr(row, "status") or STR_ACTIVE,
        "plan": _attr(row, "plan") or "standard",
        "data_region": _attr(row, "data_region") or "cn",
        "created_at": _attr(row, "created_at"),
        "updated_at": _attr(row, "updated_at"),
        "suspended_at": _attr(row, "status_changed_at")
        if (_attr(row, "status") or STR_ACTIVE) == STR_SUSPENDED
        else None,
        "suspension_reason": _attr(row, "status_reason")
        if (_attr(row, "status") or STR_ACTIVE) == STR_SUSPENDED
        else None,
    }


def _member_ref(row: Any) -> str:
    """Public WorkBuddy id of a tenant member (membership UUID string)."""
    return str(_attr(row, "membership_id") or _attr(row, "id") or "")


def _tenant_user_json(row: Any) -> dict[str, Any]:
    return {
        "id": _member_ref(row),
        "user_id": _attr(row, "user_id"),
        "username": _attr(row, "username"),
        "display_name": _attr(row, "display_name") or _attr(row, "user_display_name"),
        "email": _attr(row, "email"),
        "role": _attr(row, "role"),
        "department_id": _attr(row, "department_id"),
        "department_name": _attr(row, "department_name"),
        "status": _attr(row, "status") or STR_ACTIVE,
        "disabled": bool(_attr(row, "disabled", False)),
        "created_at": _attr(row, "joined_at") or _attr(row, "created_at"),
        "updated_at": _attr(row, "updated_at"),
    }


def _department_json(row: Any, member_count: int) -> dict[str, Any]:
    return {
        "id": str(_attr(row, "department_id")),
        "name": _attr(row, "name"),
        "description": _attr(row, "description"),
        "parent_id": _attr(row, "parent_department_id"),
        "manager_user_id": _attr(row, "manager_user_id"),
        "status": _attr(row, "status") or STR_ACTIVE,
        "member_count": member_count,
        "created_at": _attr(row, "created_at"),
        "updated_at": _attr(row, "updated_at"),
    }


def _invitation_json(row: Any) -> dict[str, Any]:
    """Invitation metadata — never carries the raw token or its hash."""
    return {
        "id": str(_attr(row, "invitation_id")),
        "email": _attr(row, "email"),
        "role": _attr(row, "role"),
        "department_id": _attr(row, "department_id"),
        "status": _attr(row, "status"),
        "expires_at": _attr(row, "expires_at"),
        "created_at": _attr(row, "created_at"),
        "invited_by": _attr(row, "invited_by"),
        "revoked_at": _attr(row, "revoked_at"),
        "accepted_at": _attr(row, "accepted_at"),
    }


def _quota_payload(rows: list[Any]) -> dict[str, Any]:
    items = [
        {
            "metric": _attr(row, "metric"),
            "limit": _attr(row, "limit"),
            "hard_cap": _attr(row, "hard_cap"),
            "used": _attr(row, "used"),
            "unit": _attr(row, "unit"),
            "updated_at": _attr(row, "updated_at"),
        }
        for row in rows
    ]
    return {
        "items": items,
        "quotas": {str(item["metric"]): item["limit"] for item in items},
        "hard_caps": {
            str(item["metric"]): item["hard_cap"] for item in items if item["hard_cap"] is not None
        },
    }


def _membership_json(principal: WorkBuddyPrincipal) -> dict[str, Any]:
    return {
        "id": principal.member_id,
        "user_id": principal.user_id,
        "role": principal.role,
        "status": principal.member_status,
        "department_id": principal.department_id,
    }


def _auth_user_json(user: User, tenant: Any, member: Any) -> dict[str, Any]:
    """Login/register payload user object: legacy Octop shape plus tenant context."""
    display_name = _attr(member, "display_name") or _attr(member, "user_display_name")
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.value,
        "display_name": display_name if display_name is not None else user.display_name,
        "locale": normalize_locale(user.locale),
        "permissions": effective_permissions(user),
        "tenant": {
            "id": str(_attr(tenant, "tenant_id")),
            "slug": _attr(tenant, "slug"),
            "name": _attr(tenant, "name"),
            "status": _attr(tenant, "status") or STR_ACTIVE,
            "role": _attr(member, "role"),
        },
    }


# --------------------------------------------------------------------------- #
# Principal and authorization dependencies
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class WorkBuddyPrincipal:
    """Authenticated Octop user plus the WorkBuddy membership derived from it."""

    user: User
    tenant_id: str
    tenant_slug: str
    tenant_name: str
    member_id: str
    role: str
    department_id: str | None
    member_status: str
    tenant_status: str

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        """Tenant-scoped admin — independent of the Octop platform admin role."""
        return self.role in TENANT_ADMIN_ROLES


@dataclass(frozen=True, slots=True)
class WorkBuddyPlatformPrincipal:
    """A caller holding an explicit ``workbuddy-platform`` audience token."""

    user: User
    audience: str
    claims: dict[str, Any]

    @property
    def user_id(self) -> int:
        return self.user.id


def _platform_secret(server: Any) -> bytes | None:
    for key in _PLATFORM_SECRET_KEYS:
        secret = server.services.secret_repo.get(key)
        if secret:
            return bytes(secret)
    return None


def _decode_platform_claims(server: Any, token: str) -> dict[str, Any]:
    """Verify signature/expiry, then require the platform audience claim."""
    secret = _platform_secret(server)
    if secret is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    try:
        claims: dict[str, Any] = jwt.decode(
            token, secret, algorithms=["HS256"], options={"verify_aud": False}
        )
    except jwt.ExpiredSignatureError as exc:
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise OctopError(ErrorCode.AUTH_FAILED, "invalid token") from exc
    audience = claims.get("aud")
    granted = [audience] if isinstance(audience, str) else list(audience or [])
    if WORKBUDDY_PLATFORM_AUDIENCE not in granted:
        raise OctopError(
            ErrorCode.WORKBUDDY_PLATFORM_AUDIENCE_REQUIRED,
            "a workbuddy-platform audience token is required",
        )
    return claims


async def workbuddy_principal(
    request: Request,
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> WorkBuddyPrincipal:
    """Derive the caller's tenant from the authenticated Octop user's membership."""
    repo = _identity_repo(server)
    member = repo.membership_for_user(user.id, active_only=False)
    if member is None:
        raise OctopError(
            ErrorCode.WORKBUDDY_MEMBERSHIP_REQUIRED,
            "no WorkBuddy tenant membership for this account",
        )
    tenant_slug = str(_attr(member, "tenant_slug") or "")
    sent_slug = (request.headers.get("x-tenant-slug") or "").strip()
    if sent_slug and sent_slug != tenant_slug:
        # The header may only confirm the derived tenant; it never selects one.
        raise OctopError(
            ErrorCode.WORKBUDDY_TENANT_MISMATCH,
            "tenant header does not match the authenticated membership",
        )
    tenant_status = str(_attr(member, "tenant_status") or STR_ACTIVE)
    if tenant_status != STR_ACTIVE:
        raise OctopError(ErrorCode.WORKBUDDY_TENANT_SUSPENDED, "tenant is suspended")
    member_status = str(_attr(member, "status") or STR_ACTIVE)
    if member_status != STR_ACTIVE:
        raise OctopError(
            ErrorCode.WORKBUDDY_MEMBER_DISABLED,
            "tenant membership is not active",
        )
    return WorkBuddyPrincipal(
        user=user,
        tenant_id=str(_attr(member, "tenant_id")),
        tenant_slug=tenant_slug,
        tenant_name=str(_attr(member, "tenant_name") or ""),
        member_id=_member_ref(member),
        role=str(_attr(member, "role") or "member"),
        department_id=_attr(member, "department_id"),
        member_status=member_status,
        tenant_status=tenant_status,
    )


def require_workbuddy_admin() -> Callable[..., Awaitable[WorkBuddyPrincipal]]:
    """Dependency factory: require the WorkBuddy tenant admin role."""

    async def _dep(
        principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    ) -> WorkBuddyPrincipal:
        if not principal.is_admin:
            raise OctopError(
                ErrorCode.FORBIDDEN,
                "workbuddy tenant admin required",
                details={"role": principal.role},
            )
        return principal

    return _dep


def require_platform_audience() -> Callable[..., Awaitable[WorkBuddyPlatformPrincipal]]:
    """Dependency factory: require an explicit ``workbuddy-platform`` audience.

    Neither the Octop platform admin role nor the tenant admin role is enough;
    the caller must present a token minted for platform management.
    """

    async def _dep(
        request: Request,
        server: Any = Depends(get_server),
    ) -> WorkBuddyPlatformPrincipal:
        raw = extract_raw_token(
            authorization=request.headers.get("authorization"),
            access_token=request.query_params.get("access_token"),
        )
        if not raw:
            raise OctopError(ErrorCode.AUTH_FAILED, "missing credentials")
        claims = _decode_platform_claims(server, raw)
        subject = claims.get("sub")
        user = server.user_manager.get_by_id(int(subject)) if subject is not None else None
        if user is None:
            raise OctopError(ErrorCode.AUTH_FAILED, "user not active")
        return WorkBuddyPlatformPrincipal(
            user=user,
            audience=WORKBUDDY_PLATFORM_AUDIENCE,
            claims=claims,
        )

    return _dep


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _require_choice(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            f"{field} must be one of: {', '.join(allowed)}",
            details={"field": field},
        )
    return value


def _require_email(value: str) -> str:
    normalized = parse_optional_email(value)
    if normalized is None:
        raise OctopError(ErrorCode.EMAIL_INVALID, "invalid email address", status=400)
    return normalized


def _require_uuid(value: str, field: str) -> str:
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            f"{field} must be a UUID string",
            details={"field": field},
        ) from exc
    return value


def _invitation_token() -> tuple[str, str]:
    """Fresh raw invitation token and the SHA-256 digest that gets persisted."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_session(server: Any, user: User, tenant: Any, member: Any) -> dict[str, Any]:
    """Mint an access token carrying informational tenant claims."""
    secret = server.services.secret_repo.get("jwt")
    if secret is None:
        raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
    ttl = int(server.services.config.access_token_ttl_seconds)
    token = sign_token(
        secret,
        sub=user.id,
        uname=user.username,
        role=user.role.value,
        ttl_seconds=ttl,
        extra_claims={
            "tnt": str(_attr(tenant, "tenant_id")),
            "tslug": _attr(tenant, "slug"),
        },
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "user": _auth_user_json(user, tenant, member),
    }


def _invalidate_credentials() -> OctopError:
    """Indistinguishable login failure for tenant, account, and password errors."""
    return OctopError(ErrorCode.AUTH_INVALID_CREDENTIALS, "invalid credentials")


def resolve_member_user_id(
    server: Any, principal: WorkBuddyPrincipal, member_id: str
) -> int | None:
    """Resolve a public tenant-member id to the Octop ``users.id``, tenant-scoped.

    Returns ``None`` for unknown or cross-tenant references; callers map that to
    a uniform 404.
    """
    repo = _identity_repo(server)
    member = repo.get_member(principal.tenant_id, member_id)
    if member is None:
        return None
    raw = _attr(member, "user_id")
    return int(raw) if raw is not None else None


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class RegisterBody(BaseModel):
    invite_token: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=128)


class TenantCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=3, max_length=32)
    owner_email: str = Field(min_length=3, max_length=254)
    plan: str = Field(default="standard", max_length=32)
    data_region: str = Field(default="cn", max_length=32)


class MemberPatchBody(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    role: str | None = None
    department_id: str | None = None
    status: str | None = None


class DepartmentCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    parent_id: str | None = None


class DepartmentPatchBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    parent_id: str | None = None
    status: str | None = None


class InvitationCreateBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(default="member", max_length=16)
    department_id: str | None = None
    expires_in_hours: int = Field(default=INVITATION_DEFAULT_HOURS, ge=1, le=INVITATION_MAX_HOURS)


class QuotasBody(BaseModel):
    quotas: dict[str, int]


class SuspendBody(BaseModel):
    reason: str | None = Field(default=None, max_length=400)


# --------------------------------------------------------------------------- #
# Public authentication
# --------------------------------------------------------------------------- #


@router.post("/auth/login", summary="WorkBuddy tenant sign-in")
@_store_errors
async def login(
    body: LoginBody,
    request: Request,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Exchange tenant slug + credentials for a tenant-scoped access token.

    Unknown tenant, unknown account, wrong password, disabled account,
    suspended tenant, and missing membership all collapse to
    ``AUTH_INVALID_CREDENTIALS``.
    """
    slug = (request.headers.get("x-tenant-slug") or "").strip()
    repo = _identity_repo(server)
    tenant = repo.get_tenant_by_slug(slug)
    if tenant is None or str(_attr(tenant, "status") or STR_ACTIVE) != STR_ACTIVE:
        raise _invalidate_credentials()
    if server.user_manager.count() == 0:
        raise _invalidate_credentials()
    user = await server.user_manager.authenticate(body.username, body.password)
    if user is None:
        raise _invalidate_credentials()
    member = repo.membership_for_user(user.id, active_only=False)
    if (
        member is None
        or str(_attr(member, "tenant_id")) != str(_attr(tenant, "tenant_id"))
        or str(_attr(member, "status") or STR_ACTIVE) != STR_ACTIVE
    ):
        raise _invalidate_credentials()
    response.headers["Cache-Control"] = "no-store"
    return workbuddy_envelope(request, _issue_session(server, user, tenant, member))


@router.post("/auth/register", status_code=201, summary="Redeem a WorkBuddy invitation")
@_store_errors
async def register(
    body: RegisterBody,
    request: Request,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Create the invited Octop account and its tenant membership in one flow.

    The invitation is email-bound: only the SHA-256 digest is stored, the
    account is created with the invited address, and the membership is added
    inside the transaction that consumes the invitation.
    """
    slug = (request.headers.get("x-tenant-slug") or "").strip()
    if not slug:
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "X-Tenant-Slug header is required",
        )
    validate_password_policy(body.password)
    repo = _identity_repo(server)
    tenant = repo.get_tenant_by_slug(slug)
    if tenant is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    tenant_id = str(_attr(tenant, "tenant_id"))
    invitation = repo.lookup_invitation(body.invite_token, require_pending=False)
    if invitation is None or str(_attr(invitation, "tenant_id")) != tenant_id:
        raise OctopError(ErrorCode.WORKBUDDY_INVITATION_INVALID, "invitation is not valid")
    status = str(_attr(invitation, "status") or "pending")
    if status == "revoked":
        raise OctopError(ErrorCode.WORKBUDDY_INVITATION_REVOKED, "invitation was revoked")
    if status == "accepted":
        raise OctopError(
            ErrorCode.WORKBUDDY_INVITATION_ALREADY_ACCEPTED,
            "invitation was already accepted",
        )
    if status == "expired":
        raise OctopError(ErrorCode.WORKBUDDY_INVITATION_EXPIRED, "invitation expired")
    user = await server.user_manager.create(
        username=body.username,
        password=body.password,
        role=Role.USER,
        display_name=body.display_name,
        email=_attr(invitation, "email"),
        locale=resolve_request_locale(request),
    )
    member = repo.add_membership(tenant_id, user.id, invitation_token=body.invite_token)
    if member is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    response.headers["Cache-Control"] = "no-store"
    return workbuddy_envelope(request, _issue_session(server, user, tenant, member))


# --------------------------------------------------------------------------- #
# Tenant context and tenant administration
# --------------------------------------------------------------------------- #


@router.get("/tenant-context", summary="Caller tenant and membership context")
@_store_errors
async def tenant_context(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Sole server-authoritative source of the caller's tenant role."""
    repo = _identity_repo(server)
    tenant = repo.get_tenant(principal.tenant_id)
    if tenant is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    return workbuddy_envelope(
        request,
        {
            "tenant": {
                "id": str(_attr(tenant, "tenant_id")),
                "name": _attr(tenant, "name"),
                "slug": _attr(tenant, "slug"),
                "status": _attr(tenant, "status") or STR_ACTIVE,
                "plan": _attr(tenant, "plan") or "standard",
                "data_region": _attr(tenant, "data_region") or "cn",
            },
            "membership": _membership_json(principal),
        },
    )


@router.post("/tenants", status_code=201, summary="Create an enterprise tenant")
@_store_errors
async def create_tenant(
    body: TenantCreateBody,
    request: Request,
    response: Response,
    actor: WorkBuddyPlatformPrincipal = Depends(require_platform_audience()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Platform-management only: create the tenant and provision its owner.

    An existing Octop account becomes the owner immediately; otherwise the
    tenant is created without an owner and a one-time owner invitation (raw
    token returned once, never persisted) is issued instead.
    """
    if not _TENANT_SLUG_RE.match(body.slug):
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "slug must be 3-32 lowercase letters, digits, or inner hyphens",
            details={"field": "slug"},
        )
    if not _PLAN_RE.match(body.plan):
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "plan has an invalid format",
            details={"field": "plan"},
        )
    if not _DATA_REGION_RE.match(body.data_region):
        raise OctopError(
            ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
            "data_region has an invalid format",
            details={"field": "data_region"},
        )
    owner_email = _require_email(body.owner_email)
    owner_row = server.services.user_repo.get_by_email(owner_email)
    if owner_row is not None and bool(_attr(owner_row, "disabled", False)):
        raise OctopError(ErrorCode.NOT_FOUND, "owner account is disabled")
    repo = _identity_repo(server)
    tenant = repo.create_tenant(
        body.slug,
        body.name,
        plan=body.plan,
        data_region=body.data_region,
        owner_user_id=int(owner_row.id) if owner_row is not None else None,
        owner_role="owner",
        created_by=actor.user_id,
    )
    assert tenant is not None, "create_tenant always returns the new tenant"
    payload: dict[str, Any] = {"tenant": _tenant_json(tenant), "admin_invitation": None}
    if owner_row is None:
        raw, digest = _invitation_token()
        invitation = repo.create_invitation(
            str(_attr(tenant, "tenant_id")),
            email=owner_email,
            token_sha256=digest,
            expires_at=int(time.time()) + INVITATION_DEFAULT_HOURS * 3600,
            role="owner",
            invited_by=actor.user_id,
        )
        if invitation is None:
            raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
        payload["admin_invitation"] = {
            **_invitation_json(invitation),
            "invite_token": raw,
        }
        response.headers["Cache-Control"] = "no-store"
    return workbuddy_envelope(request, payload)


@router.get("/tenants/{tenant_id}", summary="Tenant summary")
@_store_errors
async def get_tenant(
    tenant_id: str,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Safe tenant summary for members; management fields for tenant admins."""
    _require_uuid(tenant_id, "tenant_id")
    if tenant_id != principal.tenant_id:
        # Another tenant is invisible, not forbidden.
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    repo = _identity_repo(server)
    row = repo.get_tenant(tenant_id)
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    data: dict[str, Any] = {"tenant": _tenant_json(row), "membership": _membership_json(principal)}
    if principal.is_admin:
        users = list(repo.list_members(principal.tenant_id, limit=1000))
        departments = list(repo.list_departments(principal.tenant_id, limit=1000))
        data["member_count"] = len(users)
        data["department_count"] = len(departments)
        data["quotas"] = _quota_payload(list(repo.get_quotas(principal.tenant_id)))
    return workbuddy_envelope(request, data)


@router.post("/tenants/{tenant_id}/suspend", summary="Suspend a tenant")
@_store_errors
async def suspend_tenant(
    tenant_id: str,
    request: Request,
    body: SuspendBody | None = None,
    actor: WorkBuddyPlatformPrincipal = Depends(require_platform_audience()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Platform-management only: suspend the tenant."""
    _require_uuid(tenant_id, "tenant_id")
    repo = _identity_repo(server)
    row = repo.suspend_tenant(
        tenant_id,
        reason=body.reason if body else None,
        actor_user_id=actor.user_id,
    )
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    return workbuddy_envelope(request, {"tenant": _tenant_json(row)})


@router.post("/tenants/{tenant_id}/restore", summary="Restore a suspended tenant")
@_store_errors
async def restore_tenant(
    tenant_id: str,
    request: Request,
    body: SuspendBody | None = None,
    actor: WorkBuddyPlatformPrincipal = Depends(require_platform_audience()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Platform-management only: clear the suspension."""
    _require_uuid(tenant_id, "tenant_id")
    repo = _identity_repo(server)
    row = repo.restore_tenant(
        tenant_id,
        reason=body.reason if body else None,
        actor_user_id=actor.user_id,
    )
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    return workbuddy_envelope(request, {"tenant": _tenant_json(row)})


# --------------------------------------------------------------------------- #
# Users and departments
# --------------------------------------------------------------------------- #


@router.get("/users", summary="List tenant members")
@_store_errors
async def list_users(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant-admin projection of the tenant's members (minimal fields)."""
    repo = _identity_repo(server)
    rows = list(repo.list_members(principal.tenant_id, limit=1000))
    return workbuddy_envelope(request, [_tenant_user_json(row) for row in rows])


@router.patch("/users/{member_id}", summary="Update a tenant member")
@_store_errors
async def patch_user(
    member_id: str,
    body: MemberPatchBody,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin may change display name, tenant role, department, or status.

    A member can never promote themselves, and the identity slice refuses to
    remove the tenant's last active owner.
    """
    _require_uuid(member_id, "member_id")
    repo = _identity_repo(server)
    row = repo.get_member(principal.tenant_id, member_id)
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    provided = body.model_fields_set
    updates: dict[str, Any] = {}
    if "role" in provided and body.role is not None:
        updates["role"] = _require_choice(body.role, MEMBER_ROLES, "role")
    if "status" in provided and body.status is not None:
        updates["status"] = _require_choice(body.status, USER_STATUSES, "status")
    if "department_id" in provided:
        if body.department_id is not None:
            _require_uuid(body.department_id, "department_id")
            departments = list(repo.list_departments(principal.tenant_id, limit=1000))
            if not any(str(_attr(d, "department_id")) == body.department_id for d in departments):
                raise OctopError(ErrorCode.NOT_FOUND, "department not found")
        updates["department_id"] = body.department_id
    if "display_name" in provided:
        updates["display_name"] = body.display_name
    if _member_ref(row) == principal.member_id and updates.get("role") not in (
        None,
        principal.role,
    ):
        raise OctopError(ErrorCode.FORBIDDEN, "cannot change your own tenant role")
    updated = repo.update_member(
        principal.tenant_id, member_id, actor_user_id=principal.user_id, **updates
    )
    if updated is None:
        raise OctopError(ErrorCode.NOT_FOUND, "user not found")
    return workbuddy_envelope(request, _tenant_user_json(updated))


@router.get("/departments", summary="List tenant departments")
@_store_errors
async def list_departments(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(workbuddy_principal),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Any active member may read the tenant's department tree."""
    repo = _identity_repo(server)
    departments = list(repo.list_departments(principal.tenant_id, limit=1000))
    counts = Counter(
        str(_attr(row, "department_id") or "")
        for row in repo.list_members(principal.tenant_id, limit=1000)
    )
    return workbuddy_envelope(
        request,
        [
            _department_json(row, counts.get(str(_attr(row, "department_id")), 0))
            for row in departments
        ],
    )


@router.post("/departments", status_code=201, summary="Create a department")
@_store_errors
async def create_department(
    body: DepartmentCreateBody,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin creates a department under an existing parent (or at the root)."""
    repo = _identity_repo(server)
    if body.parent_id is not None:
        _require_uuid(body.parent_id, "parent_id")
        parents = list(repo.list_departments(principal.tenant_id, limit=1000))
        if not any(str(_attr(d, "department_id")) == body.parent_id for d in parents):
            raise OctopError(ErrorCode.NOT_FOUND, "parent department not found")
    row = repo.create_department(
        principal.tenant_id,
        name=body.name,
        parent_id=body.parent_id,
        description=body.description,
        actor_user_id=principal.user_id,
    )
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    return workbuddy_envelope(request, _department_json(row, 0))


@router.patch("/departments/{department_id}", summary="Update a department")
@_store_errors
async def patch_department(
    department_id: str,
    body: DepartmentPatchBody,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin renames, re-parents, or archives a department.

    Re-parenting that would create a cycle is rejected by the identity slice
    (``WORKBUDDY_DEPARTMENT_CYCLE``); unknown and cross-tenant ids are 404.
    """
    _require_uuid(department_id, "department_id")
    repo = _identity_repo(server)
    provided = body.model_fields_set
    updates: dict[str, Any] = {}
    if "name" in provided and body.name is not None:
        updates["name"] = body.name
    if "description" in provided:
        updates["description"] = body.description
    if "parent_id" in provided:
        if body.parent_id is not None:
            _require_uuid(body.parent_id, "parent_id")
            departments = list(repo.list_departments(principal.tenant_id, limit=1000))
            if not any(str(_attr(d, "department_id")) == body.parent_id for d in departments):
                raise OctopError(ErrorCode.NOT_FOUND, "parent department not found")
        updates["parent_id"] = body.parent_id
    if "status" in provided and body.status is not None:
        updates["status"] = _require_choice(body.status, DEPARTMENT_STATUSES, "status")
    if not updates:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "no mutable fields provided")
    row = repo.update_department(
        principal.tenant_id, department_id, actor_user_id=principal.user_id, **updates
    )
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "department not found")
    counts = Counter(
        str(_attr(user, "department_id") or "")
        for user in repo.list_members(principal.tenant_id, limit=1000)
    )
    return workbuddy_envelope(
        request, _department_json(row, counts.get(str(_attr(row, "department_id")), 0))
    )


# --------------------------------------------------------------------------- #
# Invitations
# --------------------------------------------------------------------------- #


@router.get("/invitations", summary="List tenant invitations")
@_store_errors
async def list_invitations(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Pending and historical invitations — never the raw token or its hash."""
    repo = _identity_repo(server)
    rows = list(repo.list_invitations(principal.tenant_id, limit=200))
    return workbuddy_envelope(request, [_invitation_json(row) for row in rows])


@router.post("/invitations", status_code=201, summary="Create an email-bound invitation")
@_store_errors
async def create_invitation(
    body: InvitationCreateBody,
    request: Request,
    response: Response,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin issues a one-time invitation.

    Only the SHA-256 digest of the raw token is persisted; the raw token is
    returned exactly once, under ``Cache-Control: no-store``.
    """
    email = _require_email(body.email)
    _require_choice(body.role, MEMBER_ROLES, "role")
    repo = _identity_repo(server)
    if body.department_id is not None:
        _require_uuid(body.department_id, "department_id")
        departments = list(repo.list_departments(principal.tenant_id, limit=1000))
        if not any(str(_attr(d, "department_id")) == body.department_id for d in departments):
            raise OctopError(ErrorCode.NOT_FOUND, "department not found")
    token, digest = _invitation_token()
    row = repo.create_invitation(
        principal.tenant_id,
        email=email,
        token_sha256=digest,
        expires_at=int(time.time()) + body.expires_in_hours * 3600,
        role=body.role,
        department_id=body.department_id,
        invited_by=principal.user_id,
    )
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    response.headers["Cache-Control"] = "no-store"
    return workbuddy_envelope(request, {**_invitation_json(row), "invite_token": token})


@router.post("/invitations/{invitation_id}/revoke", summary="Revoke an invitation")
@_store_errors
async def revoke_invitation(
    invitation_id: str,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin revokes an unconsumed invitation; consumed ones stay terminal."""
    _require_uuid(invitation_id, "invitation_id")
    repo = _identity_repo(server)
    row = repo.revoke_invitation(principal.tenant_id, invitation_id, revoked_by=principal.user_id)
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "invitation not found")
    return workbuddy_envelope(request, _invitation_json(row))


# --------------------------------------------------------------------------- #
# Quotas
# --------------------------------------------------------------------------- #


@router.get("/tenant-quotas", summary="Read tenant quotas")
@_store_errors
async def get_tenant_quotas(
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin reads per-metric limits, platform hard caps, and usage."""
    repo = _identity_repo(server)
    return workbuddy_envelope(request, _quota_payload(list(repo.get_quotas(principal.tenant_id))))


@router.put("/tenant-quotas", summary="Update tenant quotas")
@_store_errors
async def put_tenant_quotas(
    body: QuotasBody,
    request: Request,
    principal: WorkBuddyPrincipal = Depends(require_workbuddy_admin()),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Tenant admin tightens or adjusts quotas within the platform hard caps.

    Every metric is validated against the platform hard caps before any write,
    and the identity slice applies the whole update in one transaction so a
    rejected metric can never leave a partial update.
    """
    if not body.quotas:
        raise OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, "quotas must not be empty")
    for metric, limit in body.quotas.items():
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "quota limits must be non-negative integers",
                details={"metric": metric},
            )
    repo = _identity_repo(server)
    hard_caps = {
        str(_attr(row, "metric")): _attr(row, "hard_cap")
        for row in repo.get_quotas(principal.tenant_id)
    }
    for metric, limit in body.quotas.items():
        hard_cap = hard_caps.get(metric)
        if hard_cap is not None and limit > int(hard_cap):
            raise OctopError(
                ErrorCode.WORKBUDDY_QUOTA_EXCEEDED,
                "quota exceeds the platform hard cap",
                details={"metric": metric, "hard_cap": int(hard_cap)},
            )
    rows = repo.set_quotas(principal.tenant_id, body.quotas, actor_user_id=principal.user_id)
    if rows is None:
        raise OctopError(ErrorCode.NOT_FOUND, "tenant not found")
    return workbuddy_envelope(request, _quota_payload(list(rows)))


__all__ = [
    "WORKBUDDY_PLATFORM_AUDIENCE",
    "WorkBuddyPlatformPrincipal",
    "WorkBuddyPrincipal",
    "require_platform_audience",
    "require_workbuddy_admin",
    "resolve_member_user_id",
    "router",
    "workbuddy_envelope",
    "workbuddy_principal",
]

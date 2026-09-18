"""WorkBuddy tenant identity, membership and governance persistence (PostgreSQL only).

All public WorkBuddy identifiers (tenant, membership, department, invitation, quota,
audit event) are UUID strings; Octop user references are integer ``users.id`` values.
Timestamps are unix epoch seconds, matching the rest of the Octop control plane.

Guarantees enforced here on top of the ``015_workbuddy_identity.pg.sql`` schema:

* tenant isolation through the transaction-local ``app.*`` settings and FORCE RLS —
  scoped misses and cross-tenant references are indistinguishable and return ``None``;
* raw invitation tokens are never stored or returned: only ``sha256`` digests are
  persisted, and metadata responses never contain a password hash;
* platform hard caps for quotas, last-owner protection, department cycle/depth
  guards and an append-only governance trail.

Failures raise :class:`WorkBuddyError` (a ``ValueError``) carrying a stable English
code and a non-sensitive message; the API layer maps the code to its HTTP response.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import UNSET, DbRow, now_ts, optional_updates
from octop.infra.db.workbuddy_context import (
    WorkBuddyContextError,
    WorkBuddyDbContext,
    WorkBuddyPostgresRequiredError,
    coerce_workbuddy_context,
    normalize_user_id,
    normalize_uuid,
    require_postgres,
    workbuddy_transaction,
)

TENANT_STATUSES = ("active", "suspended")
MEMBER_ROLES = ("owner", "admin", "member")
MEMBER_STATUSES = ("active", "suspended")
DEPARTMENT_STATUSES = ("active", "archived")
INVITATION_STATUSES = ("pending", "accepted", "revoked", "expired")
DEFAULT_TENANT_PLAN = "standard"
DEFAULT_DATA_REGION = "cn"
DEFAULT_INVITATION_HOURS = 72
DEFAULT_MEMBER_ROLE = "member"
DEFAULT_NEW_USER_ROLE = "user"
INITIAL_MEMBER_ROLE = "owner"

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 200
MAX_INVITATION_HOURS = 24 * 30

ERROR_POSTGRES_REQUIRED = "DEPENDENCY_UNAVAILABLE"
ERROR_CONTEXT_INVALID = "WORKBUDDY_CONTEXT_INVALID"
ERROR_INVALID_ARGUMENT = "WORKBUDDY_INVALID_ARGUMENT"
ERROR_TENANT_SLUG_TAKEN = "WORKBUDDY_TENANT_SLUG_TAKEN"
ERROR_TENANT_SUSPENDED = "TENANT_SUSPENDED"
ERROR_MEMBERSHIP_EXISTS = "WORKBUDDY_MEMBERSHIP_EXISTS"
ERROR_LAST_OWNER_REQUIRED = "WORKBUDDY_LAST_OWNER_REQUIRED"
ERROR_DEPARTMENT_NAME_TAKEN = "WORKBUDDY_DEPARTMENT_NAME_TAKEN"
ERROR_DEPARTMENT_CYCLE = "WORKBUDDY_DEPARTMENT_CYCLE"
ERROR_DEPARTMENT_DEPTH_EXCEEDED = "WORKBUDDY_DEPARTMENT_DEPTH_EXCEEDED"
ERROR_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
ERROR_QUOTA_METRIC_INVALID = "WORKBUDDY_QUOTA_METRIC_INVALID"
ERROR_INVITATION_INVALID = "WORKBUDDY_INVITATION_INVALID"
ERROR_INVITATION_EXPIRED = "WORKBUDDY_INVITATION_EXPIRED"
ERROR_INVITATION_REVOKED = "WORKBUDDY_INVITATION_REVOKED"
ERROR_INVITATION_ALREADY_ACCEPTED = "WORKBUDDY_INVITATION_ALREADY_ACCEPTED"
ERROR_INVITATION_PENDING_EXISTS = "WORKBUDDY_INVITATION_PENDING_EXISTS"

_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_TOKEN_HASH_RE = re.compile(r"[0-9a-f]{64}")
_USERNAME_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_UNIQUE_SQLSTATE = "23505"
_CHECK_SQLSTATE = "23514"
_FK_SQLSTATE = "23503"
_PRIVILEGE_SQLSTATE = "42501"

_TENANT_COLUMNS = (
    "tenant_id, slug, name, status, plan, data_region, status_reason, status_changed_at, "
    "status_changed_by, created_by, created_at, updated_at"
)
_MEMBER_SELECT = (
    "SELECT m.membership_id, m.tenant_id, m.user_id, m.role, m.department_id, m.display_name, "
    "m.status, m.invited_by, m.joined_at, m.updated_at, "
    "u.username AS username, u.email AS user_email, u.display_name AS user_display_name, "
    "u.disabled AS user_disabled, d.name AS department_name, "
    "t.slug AS tenant_slug, t.name AS tenant_name, t.status AS tenant_status "
    "FROM workbuddy_tenant_members m "
    "JOIN users u ON u.id = m.user_id "
    "JOIN workbuddy_tenants t ON t.tenant_id = m.tenant_id "
    "LEFT JOIN workbuddy_departments d "
    "ON d.tenant_id = m.tenant_id AND d.department_id = m.department_id "
)
_DEPARTMENT_COLUMNS = (
    "department_id, tenant_id, parent_department_id, name, description, manager_user_id, "
    "status, created_by, created_at, updated_at"
)
_INVITATION_COLUMNS = (
    "invitation_id, tenant_id, email, email_normalized, role, department_id, invited_by, expires_at, "
    "created_at, revoked_at, revoked_by, accepted_at, accepted_by_user_id"
)
_PENDING_INVITATION_SQL = "accepted_at IS NULL AND revoked_at IS NULL"


class WorkBuddyError(ValueError):
    """Domain rejection carrying a stable English code (messages never contain secrets)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def generate_invitation_token() -> str:
    """Fresh raw invitation token; only its sha256 may be stored or transmitted."""
    return secrets.token_urlsafe(32)


def hash_invitation_token(token: str) -> str:
    """Lowercase sha256 hex digest of a raw invitation token."""
    cleaned = str(token or "").strip()
    if not cleaned:
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "invitation token must not be empty")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()


def _normalize_token_hash(value: object) -> str:
    text = str(value or "").strip().lower()
    if not _TOKEN_HASH_RE.fullmatch(text):
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "token_sha256 must be a sha256 hex digest")
    return text


def _normalize_slug(slug: object) -> str:
    text = str(slug or "").strip().lower().replace("_", "-")
    if not _SLUG_RE.fullmatch(text):
        raise WorkBuddyError(
            ERROR_INVALID_ARGUMENT,
            "slug must be 1-48 characters of lowercase letters, digits or hyphens",
        )
    return text


def _normalize_email(email: object) -> str:
    text = str(email or "").strip().lower()
    if len(text) > 254 or not _EMAIL_RE.fullmatch(text):
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "email address is not valid")
    return text


def _normalize_text(value: object, *, field: str, max_length: int, required: bool = True) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        if required:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must not be empty")
        return ""
    if len(text) > max_length:
        raise WorkBuddyError(
            ERROR_INVALID_ARGUMENT, f"{field} must be at most {max_length} characters"
        )
    return text


def _normalize_choice(value: object, allowed: tuple[str, ...], *, field: str) -> str:
    text = str(value or "").strip().lower()
    if text not in allowed:
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be one of {', '.join(allowed)}")
    return text


def _normalize_optional_text(value: object, *, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > max_length:
        raise WorkBuddyError(
            ERROR_INVALID_ARGUMENT, f"{field} must be at most {max_length} characters"
        )
    return text


def _normalize_timestamp(value: object, *, field: str) -> int:
    if isinstance(value, bool) or value is None:
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be a unix timestamp")
    if isinstance(value, int):
        stamp = value
    else:
        text = str(value).strip()
        if not text.isdigit():
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be a unix timestamp")
        stamp = int(text)
    if stamp <= 0:
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be a unix timestamp")
    return stamp


def _coerce_page_number(value: object, *, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be an integer")
    if isinstance(value, int):
        return value
    text_value = str(value).strip()
    if not text_value.isdigit():
        raise WorkBuddyError(ERROR_INVALID_ARGUMENT, f"{field} must be an integer")
    return int(text_value)


def _page(limit: object, offset: object) -> tuple[int, int]:
    size = _coerce_page_number(limit, field="limit", default=DEFAULT_PAGE_SIZE)
    start = _coerce_page_number(offset, field="offset", default=0)
    if size <= 0:
        size = DEFAULT_PAGE_SIZE
    size = min(size, MAX_PAGE_SIZE)
    if start < 0:
        start = 0
    return size, start


def _like_pattern(needle: str) -> str:
    escaped = needle.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _constraint_name(exc: BaseException) -> str | None:
    """Constraint or unique-index name for an integrity error (structured or from text)."""
    diag = getattr(exc, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return str(name)
    match = re.search(r'constraint "([^"]+)"', str(exc))
    if match:
        return match.group(1)
    text = str(exc)
    for candidate in (
        "workbuddy_tenants_slug_key",
        "workbuddy_departments_name_key",
        "workbuddy_members_tenant_user_key",
        "idx_workbuddy_invitations_pending_email",
        "workbuddy_invitations_token_hash_key",
    ):
        if candidate in text:
            return candidate
    return None


def _translate_db_error(exc: BaseException) -> WorkBuddyError | None:
    """Map a driver integrity error to a stable WorkBuddy rejection."""
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate == _UNIQUE_SQLSTATE:
        mapping = {
            "workbuddy_tenants_slug_key": (
                ERROR_TENANT_SLUG_TAKEN,
                "a tenant with this slug already exists",
            ),
            "workbuddy_departments_name_key": (
                ERROR_DEPARTMENT_NAME_TAKEN,
                "a department with this name already exists",
            ),
            "workbuddy_members_tenant_user_key": (
                ERROR_MEMBERSHIP_EXISTS,
                "this user is already a member of the tenant",
            ),
            "workbuddy_invitations_pending_email": (
                ERROR_INVITATION_PENDING_EXISTS,
                "a pending invitation already exists for this email",
            ),
            "idx_workbuddy_invitations_pending_email": (
                ERROR_INVITATION_PENDING_EXISTS,
                "a pending invitation already exists for this email",
            ),
            "workbuddy_invitations_token_hash_key": (
                ERROR_INVITATION_INVALID,
                "invitation token is not usable",
            ),
        }
        matched = mapping.get(_constraint_name(exc) or "")
        if matched is not None:
            return WorkBuddyError(*matched)
        return WorkBuddyError(
            ERROR_INVALID_ARGUMENT, "the request conflicts with an existing record"
        )
    if sqlstate == _CHECK_SQLSTATE:
        detail = str(exc)
        if "cycle" in detail:
            return WorkBuddyError(
                ERROR_DEPARTMENT_CYCLE, "department hierarchy would contain a cycle"
            )
        if "depth" in detail:
            return WorkBuddyError(
                ERROR_DEPARTMENT_DEPTH_EXCEEDED, "department hierarchy is nested too deeply"
            )
        if "hard cap" in detail or "unknown quota metric" in detail:
            return WorkBuddyError(
                ERROR_QUOTA_EXCEEDED, "the requested quota exceeds the platform hard cap"
            )
        return WorkBuddyError(ERROR_INVALID_ARGUMENT, "the request violates a data constraint")
    if sqlstate == _FK_SQLSTATE:
        return WorkBuddyError(
            ERROR_INVALID_ARGUMENT, "the referenced record is not part of this tenant"
        )
    if sqlstate == _PRIVILEGE_SQLSTATE:
        return WorkBuddyError(ERROR_INVALID_ARGUMENT, "the record is append only")
    return None


@contextmanager
def _db_errors() -> Iterator[None]:
    """Translate driver integrity failures; everything else propagates untouched."""
    try:
        yield
    except WorkBuddyError:
        raise
    except WorkBuddyPostgresRequiredError:
        raise
    except WorkBuddyContextError:
        raise
    except BaseException as exc:  # noqa: BLE001 - re-raised when not translatable
        translated = _translate_db_error(exc)
        if translated is None:
            raise
        raise translated from exc


def _tenant_dict(row: DbRow) -> dict[str, Any]:
    tenant_id = str(row["tenant_id"])
    return {
        "id": tenant_id,
        "tenant_id": tenant_id,
        "slug": str(row["slug"]),
        "name": str(row["name"]),
        "status": str(row["status"]),
        "plan": str(row["plan"]) if row["plan"] is not None else DEFAULT_TENANT_PLAN,
        "data_region": str(row["data_region"])
        if row["data_region"] is not None
        else DEFAULT_DATA_REGION,
        "status_reason": row["status_reason"],
        "status_changed_at": row["status_changed_at"],
        "status_changed_by": row["status_changed_by"],
        "created_by": row["created_by"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "suspended": str(row["status"]) == "suspended",
    }


def _member_dict(row: DbRow) -> dict[str, Any]:
    membership_id = str(row["membership_id"])
    return {
        "id": membership_id,
        "membership_id": membership_id,
        "tenant_id": str(row["tenant_id"]),
        "tenant_slug": row["tenant_slug"],
        "tenant_name": row["tenant_name"],
        "tenant_status": row["tenant_status"],
        "user_id": int(row["user_id"]),
        "username": row["username"],
        "email": row["user_email"],
        "user_display_name": row["user_display_name"],
        "display_name": row["display_name"],
        "role": str(row["role"]),
        "department_id": str(row["department_id"]) if row["department_id"] else None,
        "department_name": row["department_name"],
        "status": str(row["status"]),
        "invited_by": row["invited_by"],
        "joined_at": int(row["joined_at"]),
        "updated_at": int(row["updated_at"]),
        "disabled": bool(row["user_disabled"]),
    }


def _department_dict(row: DbRow) -> dict[str, Any]:
    department_id = str(row["department_id"])
    return {
        "id": department_id,
        "department_id": department_id,
        "tenant_id": str(row["tenant_id"]),
        "parent_department_id": str(row["parent_department_id"])
        if row["parent_department_id"]
        else None,
        "name": str(row["name"]),
        "description": row["description"],
        "manager_user_id": row["manager_user_id"],
        "status": str(row["status"]),
        "created_by": row["created_by"],
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _invitation_status(row: DbRow, *, now: int) -> str:
    if row["accepted_at"] is not None:
        return "accepted"
    if row["revoked_at"] is not None:
        return "revoked"
    if int(row["expires_at"]) <= now:
        return "expired"
    return "pending"


def _invitation_dict(row: DbRow, *, now: int | None = None) -> dict[str, Any]:
    stamp = now if now is not None else now_ts()
    invitation_id = str(row["invitation_id"])
    return {
        "id": invitation_id,
        "invitation_id": invitation_id,
        "tenant_id": str(row["tenant_id"]),
        "email": str(row["email"]),
        "email_normalized": str(row["email_normalized"]),
        "role": str(row["role"]),
        "department_id": str(row["department_id"]) if row["department_id"] else None,
        "status": _invitation_status(row, now=stamp),
        "invited_by": row["invited_by"],
        "expires_at": int(row["expires_at"]),
        "created_at": int(row["created_at"]),
        "revoked_at": row["revoked_at"],
        "revoked_by": row["revoked_by"],
        "accepted_at": row["accepted_at"],
        "accepted_by_user_id": row["accepted_by_user_id"],
    }


def _quota_dict(row: DbRow, *, tenant_id: str) -> dict[str, Any]:
    quota_id = str(row["quota_id"]) if row["quota_id"] else None
    return {
        "id": quota_id,
        "quota_id": quota_id,
        "tenant_id": tenant_id,
        "metric": str(row["metric"]),
        "unit": row["unit"],
        "limit": int(row["limit_value"])
        if row["limit_value"] is not None
        else int(row["default_limit"]),
        "default_limit": int(row["default_limit"]),
        "hard_cap": int(row["hard_cap"]),
        "used": int(row["used"]) if row["used"] is not None else None,
        "updated_by": row["updated_by"],
        "updated_at": int(row["updated_at"]) if row["updated_at"] is not None else None,
    }


def _audit_dict(row: DbRow) -> dict[str, Any]:
    event_id = str(row["event_id"])
    return {
        "id": event_id,
        "event_id": event_id,
        "tenant_id": str(row["tenant_id"]),
        "action": str(row["action"]),
        "actor_user_id": row["actor_user_id"],
        "reason": row["reason"],
        "detail": row["detail"],
        "created_at": int(row["created_at"]),
    }


class WorkBuddyIdentityRepo:
    """Tenant identity, membership, department, invitation and quota persistence.

    Construction is cheap (the pool is stored, nothing is checked out) but fails
    closed on non-PostgreSQL pools, so building it per request is fine. Every method
    owns its transaction; pass ``conn=`` to join an ambient
    :func:`~octop.infra.db.workbuddy_context.workbuddy_transaction` instead.
    """

    def __init__(self, db: DatabasePool) -> None:
        require_postgres(db)
        self._db = db

    # ── context helpers ──────────────────────────────────────────────────────

    def _platform_context(
        self, ctx: Any = None, *, tenant_id: object = None, user_id: object = None
    ) -> WorkBuddyDbContext:
        if ctx is not None:
            return coerce_workbuddy_context(ctx)
        return WorkBuddyDbContext.platform(tenant_id=tenant_id, user_id=user_id)

    def _tenant_context(
        self,
        tenant_id: object,
        *,
        ctx: Any = None,
        user_id: object = None,
        department_id: object = None,
    ) -> WorkBuddyDbContext:
        if ctx is not None:
            return coerce_workbuddy_context(ctx)
        return WorkBuddyDbContext.for_tenant(
            tenant_id, user_id=user_id, department_id=department_id
        )

    def _transaction(self, ctx: WorkBuddyDbContext, conn: Any = None) -> Any:
        if conn is not None:
            return _AmbientTransaction(conn)
        return workbuddy_transaction(self._db, ctx)

    # ── tenants ──────────────────────────────────────────────────────────────

    def create_tenant(
        self,
        slug: object,
        name: object,
        *,
        plan: object = DEFAULT_TENANT_PLAN,
        data_region: object = DEFAULT_DATA_REGION,
        owner_user_id: object = None,
        owner_role: str = INITIAL_MEMBER_ROLE,
        created_by: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        slug_normalized = _normalize_slug(slug)
        display_name = _normalize_text(name, field="name", max_length=120)
        plan_value = _normalize_text(plan, field="plan", max_length=40)
        region_value = _normalize_text(data_region, field="data_region", max_length=40)
        role = _normalize_choice(owner_role, MEMBER_ROLES, field="owner_role")
        owner = (
            normalize_user_id(owner_user_id, field="owner_user_id")
            if owner_user_id is not None
            else None
        )
        actor = (
            normalize_user_id(created_by, field="created_by") if created_by is not None else None
        )
        tenant_id = str(uuid.uuid4())
        stamp = now_ts()
        context = self._platform_context(ctx, tenant_id=tenant_id, user_id=owner or actor)
        with _db_errors(), self._transaction(context, conn) as active:
            active.execute(
                "INSERT INTO workbuddy_tenants (tenant_id, slug, slug_normalized, name, status, plan, "
                "data_region, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    slug_normalized,
                    slug_normalized,
                    display_name,
                    plan_value,
                    region_value,
                    actor or owner,
                    stamp,
                    stamp,
                ),
            )
            self._seed_quotas(active, tenant_id, stamp)
            if owner is not None:
                self._insert_membership(
                    active,
                    tenant_id=tenant_id,
                    user_id=owner,
                    role=role,
                    department_id=None,
                    display_name=None,
                    status="active",
                    invited_by=None,
                    stamp=stamp,
                )
            self._audit(
                active,
                tenant_id,
                "tenant.created",
                actor_user_id=actor or owner,
                reason=None,
                detail=f"slug={slug_normalized}",
            )
            row = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return _tenant_dict(row)

    def get_tenant(
        self, tenant_id: object, *, ctx: Any = None, conn: Any = None
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        context = self._platform_context(ctx, tenant_id=identifier)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE tenant_id = ?",
                (identifier,),
            ).fetchone()
        return _tenant_dict(row) if row else None

    def get_tenant_by_slug(
        self, slug: object, *, ctx: Any = None, conn: Any = None
    ) -> dict[str, Any] | None:
        slug_normalized = _normalize_slug(slug)
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE slug_normalized = ?",
                (slug_normalized,),
            ).fetchone()
        return _tenant_dict(row) if row else None

    def list_tenants(
        self,
        *,
        status: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        size, start = _page(limit, offset)
        clauses = ""
        params: list[object] = []
        if status is not None:
            clauses = " WHERE status = ?"
            params.append(_normalize_choice(status, TENANT_STATUSES, field="status"))
        params.extend([size, start])
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants{clauses} "
                "ORDER BY created_at DESC, tenant_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [_tenant_dict(row) for row in rows]

    def set_tenant_status(
        self,
        tenant_id: object,
        status: object,
        *,
        reason: object = None,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        target_status = _normalize_choice(status, TENANT_STATUSES, field="status")
        note = _normalize_optional_text(reason, field="reason", max_length=400)
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        context = self._platform_context(ctx, tenant_id=identifier, user_id=actor)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            current = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE tenant_id = ?",
                (identifier,),
            ).fetchone()
            if current is None:
                return None
            if str(current["status"]) == target_status:
                return _tenant_dict(current)
            active.execute(
                "UPDATE workbuddy_tenants SET status = ?, status_reason = ?, status_changed_at = ?, "
                "status_changed_by = ?, updated_at = ? WHERE tenant_id = ?",
                (target_status, note, stamp, actor, stamp, identifier),
            )
            self._audit(
                active,
                identifier,
                "tenant.suspended" if target_status == "suspended" else "tenant.restored",
                actor_user_id=actor,
                reason=note,
                detail=None,
            )
            row = active.execute(
                f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE tenant_id = ?",
                (identifier,),
            ).fetchone()
        return _tenant_dict(row)

    def suspend_tenant(
        self,
        tenant_id: object,
        *,
        reason: object = None,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        return self.set_tenant_status(
            tenant_id,
            "suspended",
            reason=reason,
            actor_user_id=actor_user_id,
            ctx=ctx,
            conn=conn,
        )

    def restore_tenant(
        self,
        tenant_id: object,
        *,
        reason: object = None,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        return self.set_tenant_status(
            tenant_id,
            "active",
            reason=reason,
            actor_user_id=actor_user_id,
            ctx=ctx,
            conn=conn,
        )

    # ── memberships ──────────────────────────────────────────────────────────

    def membership_for_user(
        self,
        octop_user_id: object,
        *,
        active_only: bool = True,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        user_id = normalize_user_id(octop_user_id, field="octop_user_id")
        clauses = " WHERE m.user_id = ?"
        if active_only:
            clauses += " AND m.status = 'active'"
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"{_MEMBER_SELECT}{clauses} "
                "ORDER BY CASE WHEN t.status = 'active' THEN 0 ELSE 1 END, m.joined_at, m.membership_id "
                "LIMIT 1",
                (user_id,),
            ).fetchone()
        return _member_dict(row) if row else None

    def tenant_for_user(
        self,
        octop_user_id: object,
        *,
        active_only: bool = True,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        user_id = normalize_user_id(octop_user_id, field="octop_user_id")
        clauses = " WHERE m.user_id = ?"
        if active_only:
            clauses += " AND m.status = 'active'"
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                f"{_MEMBER_SELECT}{clauses} ORDER BY m.joined_at, m.membership_id",
                (user_id,),
            ).fetchall()
        return [_member_dict(row) for row in rows]

    def add_membership(
        self,
        tenant_id: object,
        octop_user_id: object,
        *,
        role: object = None,
        department_id: object = None,
        status: str = "active",
        display_name: object = None,
        invitation_token: object = None,
        invited_by: object = None,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        user_id = normalize_user_id(octop_user_id, field="octop_user_id")
        member_status = _normalize_choice(status, MEMBER_STATUSES, field="status")
        department = normalize_uuid(department_id, field="department_id") if department_id else None
        override = _normalize_optional_text(display_name, field="display_name", max_length=120)
        inviter = (
            normalize_user_id(invited_by, field="invited_by") if invited_by is not None else None
        )
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        token_hash = hash_invitation_token(str(invitation_token)) if invitation_token else None
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor or user_id)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            tenant = self._load_tenant_row(active, identifier)
            if tenant is None:
                return None
            self._require_active_tenant(tenant)
            user = active.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                return None
            member_role = (
                _normalize_choice(role, MEMBER_ROLES, field="role") if role is not None else None
            )
            if token_hash is not None:
                invitation = self._load_invitation(active, token_hash)
                self._require_pending_invitation(invitation, now=stamp)
                assert invitation is not None
                if member_role is None:
                    member_role = str(invitation["role"])
                if department is None and invitation["department_id"]:
                    department = str(invitation["department_id"])
                if inviter is None and invitation["invited_by"]:
                    inviter = int(invitation["invited_by"])
                self._require_invitation_email(invitation, user["email"])
            if member_role is None:
                member_role = DEFAULT_MEMBER_ROLE
            if department is not None and not self._department_exists(
                active, identifier, department
            ):
                return None
            duplicate = active.execute(
                "SELECT 1 AS present FROM workbuddy_tenant_members WHERE tenant_id = ? AND user_id = ?",
                (identifier, user_id),
            ).fetchone()
            if duplicate is not None:
                raise WorkBuddyError(
                    ERROR_MEMBERSHIP_EXISTS, "this user is already a member of the tenant"
                )
            self._enforce_user_quota(active, identifier, additional=1)
            membership_id = self._insert_membership(
                active,
                tenant_id=identifier,
                user_id=user_id,
                role=member_role,
                department_id=department,
                display_name=override,
                status=member_status,
                invited_by=inviter,
                stamp=stamp,
            )
            if token_hash is not None and invitation is not None:
                active.execute(
                    "UPDATE workbuddy_invitations SET accepted_at = ?, accepted_by_user_id = ? "
                    "WHERE invitation_id = ?",
                    (stamp, user_id, str(invitation["invitation_id"])),
                )
                self._audit(
                    active,
                    identifier,
                    "invitation.accepted",
                    actor_user_id=user_id,
                    reason=None,
                    detail=f"invitation_id={invitation['invitation_id']}",
                )
            self._audit(
                active,
                identifier,
                "member.added",
                actor_user_id=actor or inviter or user_id,
                reason=None,
                detail=f"user_id={user_id};role={member_role}",
            )
            row = self._load_member_row(active, identifier, membership_id)
        return _member_dict(row) if row else None

    def list_members(
        self,
        tenant_id: object,
        *,
        status: object = None,
        department_id: object = None,
        query: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        size, start = _page(limit, offset)
        clauses = [" WHERE m.tenant_id = ?"]
        params: list[object] = [identifier]
        if status is not None:
            clauses.append(" AND m.status = ?")
            params.append(_normalize_choice(status, MEMBER_STATUSES, field="status"))
        if department_id is not None:
            clauses.append(" AND m.department_id = ?")
            params.append(normalize_uuid(department_id, field="department_id"))
        if query is not None and str(query).strip():
            clauses.append(
                " AND (lower(u.username) LIKE ? ESCAPE '\\' OR lower(coalesce(u.email, '')) LIKE ? ESCAPE '\\'"
                " OR lower(coalesce(u.display_name, '')) LIKE ? ESCAPE '\\'"
                " OR lower(coalesce(m.display_name, '')) LIKE ? ESCAPE '\\')"
            )
            pattern = _like_pattern(str(query).strip())
            params.extend([pattern, pattern, pattern, pattern])
        params.extend([size, start])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                f"{_MEMBER_SELECT}{''.join(clauses)} ORDER BY m.joined_at, m.membership_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [_member_dict(row) for row in rows]

    def get_member(
        self,
        tenant_id: object,
        member_id: object,
        *,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        membership = normalize_uuid(member_id, field="member_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = self._load_member_row(active, identifier, membership)
        return _member_dict(row) if row else None

    def get_member_by_user_id(
        self,
        tenant_id: object,
        octop_user_id: object,
        *,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        user_id = normalize_user_id(octop_user_id, field="octop_user_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"{_MEMBER_SELECT} WHERE m.tenant_id = ? AND m.user_id = ?",
                (identifier, user_id),
            ).fetchone()
        return _member_dict(row) if row else None

    def update_member(
        self,
        tenant_id: object,
        member_id: object,
        *,
        role: object = UNSET,
        department_id: object = UNSET,
        status: object = UNSET,
        display_name: object = UNSET,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        membership = normalize_uuid(member_id, field="member_id")
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        if role is not UNSET:
            role = _normalize_choice(role, MEMBER_ROLES, field="role")
        if status is not UNSET:
            status = _normalize_choice(status, MEMBER_STATUSES, field="status")
        department = UNSET
        if department_id is not UNSET:
            department = (
                normalize_uuid(department_id, field="department_id") if department_id else None
            )
        override: object = UNSET
        if display_name is not UNSET:
            override = _normalize_optional_text(display_name, field="display_name", max_length=120)
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            current = self._load_member_row(active, identifier, membership)
            if current is None:
                return None
            if isinstance(department, str) and not self._department_exists(
                active, identifier, department
            ):
                return None
            next_role = str(current["role"]) if role is UNSET else str(role)
            next_status = str(current["status"]) if status is UNSET else str(status)
            if (
                str(current["role"]) == "owner"
                and str(current["status"]) == "active"
                and (next_role != "owner" or next_status != "active")
            ):
                self._require_other_owner(active, identifier, membership)
            clauses, params = optional_updates(
                [
                    ("role", role),
                    ("department_id", department),
                    ("status", status),
                    ("display_name", override),
                ]
            )
            if not clauses:
                return _member_dict(current)
            clauses.append("updated_at = ?")
            params.append(stamp)
            params.extend([identifier, membership])
            active.execute(
                f"UPDATE workbuddy_tenant_members SET {', '.join(clauses)} "
                "WHERE tenant_id = ? AND membership_id = ?",
                tuple(params),
            )
            self._audit(
                active,
                identifier,
                "member.updated",
                actor_user_id=actor,
                reason=None,
                detail=f"membership_id={membership}",
            )
            row = self._load_member_row(active, identifier, membership)
        return _member_dict(row) if row else None

    # Ticket-facing aliases: membership rows are the tenant-scoped user records.

    def list_users(
        self,
        tenant_id: object,
        *,
        status: object = None,
        department_id: object = None,
        query: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        return self.list_members(
            tenant_id,
            status=status,
            department_id=department_id,
            query=query,
            limit=limit,
            offset=offset,
            ctx=ctx,
            conn=conn,
        )

    patch_user = update_member

    # ── departments ──────────────────────────────────────────────────────────

    def list_departments(
        self,
        tenant_id: object,
        *,
        status: object = None,
        parent_id: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        size, start = _page(limit, offset)
        clauses = [" WHERE tenant_id = ?"]
        params: list[object] = [identifier]
        if status is not None:
            clauses.append(" AND status = ?")
            params.append(_normalize_choice(status, DEPARTMENT_STATUSES, field="status"))
        if parent_id is not None:
            clauses.append(" AND parent_department_id = ?")
            params.append(normalize_uuid(parent_id, field="parent_id"))
        params.extend([size, start])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                f"SELECT {_DEPARTMENT_COLUMNS} FROM workbuddy_departments{''.join(clauses)} "
                "ORDER BY name_normalized, department_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [_department_dict(row) for row in rows]

    def create_department(
        self,
        tenant_id: object,
        *,
        name: object,
        parent_id: object = None,
        description: object = None,
        manager_user_id: object = None,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        display_name = _normalize_text(name, field="name", max_length=80)
        normalized = display_name.lower()
        parent = normalize_uuid(parent_id, field="parent_id") if parent_id else None
        notes = _normalize_optional_text(description, field="description", max_length=1000)
        manager = (
            normalize_user_id(manager_user_id, field="manager_user_id")
            if manager_user_id is not None
            else None
        )
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        department_id = str(uuid.uuid4())
        stamp = now_ts()
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor)
        with _db_errors(), self._transaction(context, conn) as active:
            tenant = self._load_tenant_row(active, identifier)
            if tenant is None:
                return None
            self._require_active_tenant(tenant)
            if parent is not None and not self._guard_department_parent(
                active, identifier, department_id, parent
            ):
                return None
            active.execute(
                "INSERT INTO workbuddy_departments (department_id, tenant_id, parent_department_id, name, "
                "name_normalized, description, manager_user_id, status, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    department_id,
                    identifier,
                    parent,
                    display_name,
                    normalized,
                    notes,
                    manager,
                    actor,
                    stamp,
                    stamp,
                ),
            )
            self._audit(
                active,
                identifier,
                "department.created",
                actor_user_id=actor,
                reason=None,
                detail=f"department_id={department_id}",
            )
            row = active.execute(
                f"SELECT {_DEPARTMENT_COLUMNS} FROM workbuddy_departments WHERE department_id = ?",
                (department_id,),
            ).fetchone()
        return _department_dict(row) if row else None

    def update_department(
        self,
        tenant_id: object,
        department_id: object,
        *,
        name: object = UNSET,
        description: object = UNSET,
        parent_id: object = UNSET,
        manager_user_id: object = UNSET,
        status: object = UNSET,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        target = normalize_uuid(department_id, field="department_id")
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        new_name: object = UNSET
        if name is not UNSET:
            text = _normalize_text(name, field="name", max_length=80)
            new_name = (text, text.lower())
        new_notes: object = UNSET
        if description is not UNSET:
            new_notes = _normalize_optional_text(description, field="description", max_length=1000)
        new_parent: object = UNSET
        if parent_id is not UNSET:
            new_parent = normalize_uuid(parent_id, field="parent_id") if parent_id else None
        new_manager: object = UNSET
        if manager_user_id is not UNSET:
            new_manager = (
                normalize_user_id(manager_user_id, field="manager_user_id")
                if manager_user_id is not None
                else None
            )
        new_status: object = UNSET
        if status is not UNSET:
            new_status = _normalize_choice(status, DEPARTMENT_STATUSES, field="status")
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            current = active.execute(
                f"SELECT {_DEPARTMENT_COLUMNS} FROM workbuddy_departments "
                "WHERE tenant_id = ? AND department_id = ?",
                (identifier, target),
            ).fetchone()
            if current is None:
                return None
            if isinstance(new_parent, str) and not self._guard_department_parent(
                active, identifier, target, new_parent
            ):
                return None
            clauses: list[str] = []
            params: list[object] = []
            if isinstance(new_name, tuple):
                clauses.extend(["name = ?", "name_normalized = ?"])
                params.extend(new_name)
            if new_notes is not UNSET:
                clauses.append("description = ?")
                params.append(new_notes)
            if new_parent is not UNSET:
                clauses.append("parent_department_id = ?")
                params.append(new_parent)
            if new_manager is not UNSET:
                clauses.append("manager_user_id = ?")
                params.append(new_manager)
            if new_status is not UNSET:
                clauses.append("status = ?")
                params.append(new_status)
            if not clauses:
                return _department_dict(current)
            clauses.append("updated_at = ?")
            params.append(stamp)
            params.extend([identifier, target])
            active.execute(
                f"UPDATE workbuddy_departments SET {', '.join(clauses)} "
                "WHERE tenant_id = ? AND department_id = ?",
                tuple(params),
            )
            self._audit(
                active,
                identifier,
                "department.updated",
                actor_user_id=actor,
                reason=None,
                detail=f"department_id={target}",
            )
            row = active.execute(
                f"SELECT {_DEPARTMENT_COLUMNS} FROM workbuddy_departments "
                "WHERE tenant_id = ? AND department_id = ?",
                (identifier, target),
            ).fetchone()
        return _department_dict(row) if row else None

    patch_department = update_department

    # ── invitations ──────────────────────────────────────────────────────────

    def list_invitations(
        self,
        tenant_id: object,
        *,
        status: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        size, start = _page(limit, offset)
        clauses = [" WHERE tenant_id = ?"]
        params: list[object] = [identifier]
        if status is not None:
            target = _normalize_choice(status, INVITATION_STATUSES, field="status")
            if target == "accepted":
                clauses.append(" AND accepted_at IS NOT NULL")
            elif target == "revoked":
                clauses.append(" AND revoked_at IS NOT NULL")
            elif target == "expired":
                clauses.append(f" AND {_PENDING_INVITATION_SQL} AND expires_at <= ?")
                params.append(now_ts())
            else:
                clauses.append(f" AND {_PENDING_INVITATION_SQL} AND expires_at > ?")
                params.append(now_ts())
        params.extend([size, start])
        context = self._tenant_context(identifier, ctx=ctx)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                f"SELECT {_INVITATION_COLUMNS} FROM workbuddy_invitations{''.join(clauses)} "
                "ORDER BY created_at DESC, invitation_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [_invitation_dict(row, now=stamp) for row in rows]

    def create_invitation(
        self,
        tenant_id: object,
        *,
        email: object,
        token_sha256: object,
        expires_at: object,
        role: str = DEFAULT_MEMBER_ROLE,
        department_id: object = None,
        invited_by: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        address = _normalize_email(email)
        token_hash = _normalize_token_hash(token_sha256)
        expiry = _normalize_timestamp(expires_at, field="expires_at")
        member_role = _normalize_choice(role, MEMBER_ROLES, field="role")
        department = normalize_uuid(department_id, field="department_id") if department_id else None
        inviter = (
            normalize_user_id(invited_by, field="invited_by") if invited_by is not None else None
        )
        stamp = now_ts()
        if expiry <= stamp:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "expires_at must be in the future")
        context = self._tenant_context(identifier, ctx=ctx, user_id=inviter)
        with _db_errors(), self._transaction(context, conn) as active:
            tenant = self._load_tenant_row(active, identifier)
            if tenant is None:
                return None
            self._require_active_tenant(tenant)
            if department is not None and not self._department_exists(
                active, identifier, department
            ):
                return None
            invitation_id = str(uuid.uuid4())
            active.execute(
                "INSERT INTO workbuddy_invitations (invitation_id, tenant_id, email, email_normalized, role, "
                "department_id, token_hash, invited_by, expires_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invitation_id,
                    identifier,
                    address,
                    address,
                    member_role,
                    department,
                    token_hash,
                    inviter,
                    expiry,
                    stamp,
                ),
            )
            self._audit(
                active,
                identifier,
                "invitation.created",
                actor_user_id=inviter,
                reason=None,
                detail=f"invitation_id={invitation_id};role={member_role}",
            )
            row = active.execute(
                f"SELECT {_INVITATION_COLUMNS} FROM workbuddy_invitations WHERE invitation_id = ?",
                (invitation_id,),
            ).fetchone()
        return _invitation_dict(row, now=stamp) if row else None

    def revoke_invitation(
        self,
        tenant_id: object,
        invitation_id: object,
        *,
        revoked_by: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        target = normalize_uuid(invitation_id, field="invitation_id")
        actor = (
            normalize_user_id(revoked_by, field="revoked_by") if revoked_by is not None else None
        )
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            current = self._load_invitation_row(active, identifier, target)
            if current is None:
                return None
            if current["accepted_at"] is not None:
                raise WorkBuddyError(
                    ERROR_INVITATION_ALREADY_ACCEPTED, "the invitation has already been accepted"
                )
            if current["revoked_at"] is not None:
                return _invitation_dict(current, now=stamp)
            active.execute(
                "UPDATE workbuddy_invitations SET revoked_at = ?, revoked_by = ? WHERE invitation_id = ?",
                (stamp, actor, target),
            )
            self._audit(
                active,
                identifier,
                "invitation.revoked",
                actor_user_id=actor,
                reason=None,
                detail=f"invitation_id={target}",
            )
            row = self._load_invitation_row(active, identifier, target)
        return _invitation_dict(row, now=stamp) if row else None

    def lookup_invitation(
        self,
        token: object,
        *,
        require_pending: bool = True,
        token_is_hash: bool = False,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        token_hash = (
            _normalize_token_hash(token)
            if token_is_hash
            else hash_invitation_token(str(token or ""))
        )
        stamp = now_ts()
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = self._load_invitation(active, token_hash)
            if row is None:
                return None
            if require_pending and not self._invitation_usable(row, now=stamp):
                return None
        return _invitation_dict(row, now=stamp)

    def consume_invitation(
        self,
        token_sha256: object,
        display_name: object = None,
        password_hash: object = None,
        *,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Atomically consume a pending invitation and bind or create the Octop user.

        The pending invitation is consumed in the same transaction that creates (or
        binds) the global ``users`` row and the tenant membership, so a failed redeem
        leaves no orphaned user. Only identifiers are returned — never a password hash.
        """
        token_hash = _normalize_token_hash(token_sha256)
        secret = str(password_hash or "").strip()
        if not secret:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "password_hash is required")
        if len(secret) > 512:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "password_hash is not a valid digest")
        person = _normalize_optional_text(display_name, field="display_name", max_length=120)
        context = self._platform_context(ctx)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            invitation = self._load_invitation(active, token_hash)
            self._require_pending_invitation(invitation, now=stamp)
            assert invitation is not None
            tenant_id = str(invitation["tenant_id"])
            tenant = self._load_tenant_row(active, tenant_id)
            if tenant is None:
                raise WorkBuddyError(ERROR_INVITATION_INVALID, "invitation token is not usable")
            self._require_active_tenant(tenant)
            address = str(invitation["email_normalized"])
            user = active.execute(
                "SELECT id, username, email, display_name, disabled FROM users "
                "WHERE lower(coalesce(email, '')) = ? ORDER BY id LIMIT 1",
                (address,),
            ).fetchone()
            created_user = False
            if user is None:
                username = self._available_username(active, address, person)
                user = active.execute(
                    "INSERT INTO users (username, password_hash, role, display_name, disabled, locale, "
                    "email, created_at, permissions) VALUES (?, ?, ?, ?, 0, 'zh', ?, ?, '[]') "
                    "RETURNING id, username, email, display_name, disabled",
                    (
                        username,
                        secret,
                        DEFAULT_NEW_USER_ROLE,
                        person or address.split("@")[0],
                        address,
                        stamp,
                    ),
                ).fetchone()
                created_user = True
            else:
                if bool(user["disabled"]):
                    raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "the invited account is disabled")
                user = active.execute(
                    "UPDATE users SET email = COALESCE(NULLIF(email, ''), ?) WHERE id = ? "
                    "RETURNING id, username, email, display_name, disabled",
                    (address, int(user["id"])),
                ).fetchone()
            user_id = int(user["id"])
            self._enforce_user_quota(active, tenant_id, additional=1)
            membership_id = self._insert_membership(
                active,
                tenant_id=tenant_id,
                user_id=user_id,
                role=str(invitation["role"]),
                department_id=str(invitation["department_id"])
                if invitation["department_id"]
                else None,
                display_name=None,
                status="active",
                invited_by=int(invitation["invited_by"]) if invitation["invited_by"] else None,
                stamp=stamp,
            )
            active.execute(
                "UPDATE workbuddy_invitations SET accepted_at = ?, accepted_by_user_id = ? "
                "WHERE invitation_id = ?",
                (stamp, user_id, str(invitation["invitation_id"])),
            )
            self._audit(
                active,
                tenant_id,
                "invitation.accepted",
                actor_user_id=user_id,
                reason=None,
                detail=f"invitation_id={invitation['invitation_id']}",
            )
            member_row = self._load_member_row(active, tenant_id, membership_id)
            consumed = self._load_invitation(active, token_hash)
        return {
            "created_user": created_user,
            "user": {
                "id": user_id,
                "username": user["username"],
                "email": user["email"],
                "display_name": user["display_name"],
            },
            "membership": _member_dict(member_row) if member_row else None,
            "invitation": _invitation_dict(consumed or invitation, now=stamp),
        }

    # ── quotas ───────────────────────────────────────────────────────────────

    def get_quotas(
        self, tenant_id: object, *, ctx: Any = None, conn: Any = None
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = self._quota_rows(active, identifier)
        return [_quota_dict(row, tenant_id=identifier) for row in rows]

    get_quota = get_quotas

    def set_quotas(
        self,
        tenant_id: object,
        quotas: Mapping[str, Any],
        *,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        actor = (
            normalize_user_id(actor_user_id, field="actor_user_id")
            if actor_user_id is not None
            else None
        )
        if not isinstance(quotas, Mapping) or not quotas:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "quotas must be a non-empty mapping")
        context = self._tenant_context(identifier, ctx=ctx, user_id=actor)
        stamp = now_ts()
        with _db_errors(), self._transaction(context, conn) as active:
            tenant = self._load_tenant_row(active, identifier)
            if tenant is None:
                return None
            catalogue = {
                str(row["metric"]): row
                for row in active.execute(
                    "SELECT metric, hard_cap, default_limit FROM workbuddy_quota_metrics"
                ).fetchall()
            }
            updates: list[tuple[str, int]] = []
            for metric, raw in quotas.items():
                name = str(metric or "").strip()
                if name not in catalogue:
                    raise WorkBuddyError(
                        ERROR_QUOTA_METRIC_INVALID, f"unknown quota metric {name or '(empty)'}"
                    )
                limit = _normalize_limit(raw, metric=name)
                hard_cap = int(catalogue[name]["hard_cap"])
                if limit > hard_cap:
                    raise WorkBuddyError(
                        ERROR_QUOTA_EXCEEDED,
                        f"limit for {name} exceeds the platform hard cap of {hard_cap}",
                    )
                updates.append((name, limit))
            for name, limit in updates:
                active.execute(
                    "INSERT INTO workbuddy_tenant_quotas (quota_id, tenant_id, metric, limit_value, "
                    "updated_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (tenant_id, metric) DO UPDATE SET limit_value = EXCLUDED.limit_value, "
                    "updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                    (str(uuid.uuid4()), identifier, name, limit, actor, stamp, stamp),
                )
            self._audit(
                active,
                identifier,
                "quota.updated",
                actor_user_id=actor,
                reason=None,
                detail=json.dumps(dict(updates), sort_keys=True),
            )
            rows = self._quota_rows(active, identifier)
        return [_quota_dict(row, tenant_id=identifier) for row in rows]

    def update_quota(
        self,
        tenant_id: object,
        metric: object,
        limit: object,
        *,
        actor_user_id: object = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any] | None:
        name = str(metric or "").strip()
        rows = self.set_quotas(
            tenant_id,
            {name: limit},
            actor_user_id=actor_user_id,
            ctx=ctx,
            conn=conn,
        )
        if rows is None:
            return None
        for row in rows:
            if row["metric"] == name:
                return row
        raise WorkBuddyError(
            ERROR_QUOTA_METRIC_INVALID, f"unknown quota metric {name or '(empty)'}"
        )

    # ── governance trail ─────────────────────────────────────────────────────

    def list_audit_events(
        self,
        tenant_id: object,
        *,
        action: object = None,
        limit: object = DEFAULT_PAGE_SIZE,
        offset: object = 0,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        size, start = _page(limit, offset)
        clauses = [" WHERE tenant_id = ?"]
        params: list[object] = [identifier]
        if action is not None and str(action).strip():
            clauses.append(" AND action = ?")
            params.append(str(action).strip())
        params.extend([size, start])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            rows = active.execute(
                "SELECT event_id, tenant_id, action, actor_user_id, reason, detail, created_at "
                f"FROM workbuddy_tenant_audit_events{''.join(clauses)} "
                "ORDER BY created_at DESC, event_id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [_audit_dict(row) for row in rows]

    # ── login discovery ──────────────────────────────────────────────────────

    def login_discovery(
        self, identifier: object, *, ctx: Any = None, conn: Any = None
    ) -> dict[str, Any]:
        """Resolve a login identifier to its tenants — identifiers and status only.

        Never returns ``password_hash``; the caller performs password verification
        through the Octop user manager.
        """
        needle = str(identifier or "").strip()
        if not needle:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "identifier must not be empty")
        context = self._platform_context(ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                "SELECT id, username, display_name, email, disabled FROM users "
                "WHERE username = ? OR lower(coalesce(email, '')) = ? ORDER BY id LIMIT 1",
                (needle, needle.lower()),
            ).fetchone()
            if row is None:
                return {
                    "found": False,
                    "status": "not_found",
                    "user_id": None,
                    "username": None,
                    "display_name": None,
                    "email": None,
                    "email_verified": False,
                    "tenants": [],
                }
            user_id = int(row["id"])
            memberships = active.execute(
                f"{_MEMBER_SELECT} WHERE m.user_id = ? ORDER BY m.joined_at, m.membership_id",
                (user_id,),
            ).fetchall()
        return {
            "found": True,
            "status": "disabled" if bool(row["disabled"]) else "active",
            "user_id": user_id,
            "username": row["username"],
            "display_name": row["display_name"],
            "email": row["email"],
            "email_verified": bool(row["email"]),
            "tenants": [_member_dict(member) for member in memberships],
        }

    # ── internal helpers ─────────────────────────────────────────────────────

    def _load_tenant_row(self, conn: Any, tenant_id: str) -> DbRow | None:
        row: DbRow | None = conn.execute(
            f"SELECT {_TENANT_COLUMNS} FROM workbuddy_tenants WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        return row

    def _require_active_tenant(self, tenant: DbRow) -> None:
        if str(tenant["status"]) != "active":
            raise WorkBuddyError(ERROR_TENANT_SUSPENDED, "the tenant is suspended")

    def _insert_membership(
        self,
        conn: Any,
        *,
        tenant_id: str,
        user_id: int,
        role: str,
        department_id: str | None,
        display_name: str | None,
        status: str,
        invited_by: int | None,
        stamp: int,
    ) -> str:
        membership_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO workbuddy_tenant_members (membership_id, tenant_id, user_id, role, department_id, "
            "display_name, status, invited_by, joined_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                membership_id,
                tenant_id,
                user_id,
                role,
                department_id,
                display_name,
                status,
                invited_by,
                stamp,
                stamp,
            ),
        )
        return membership_id

    def _load_member_row(self, conn: Any, tenant_id: str, membership_id: str) -> DbRow | None:
        row: DbRow | None = conn.execute(
            f"{_MEMBER_SELECT} WHERE m.tenant_id = ? AND m.membership_id = ?",
            (tenant_id, membership_id),
        ).fetchone()
        return row

    def _department_exists(self, conn: Any, tenant_id: str, department_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 AS present FROM workbuddy_departments WHERE tenant_id = ? AND department_id = ?",
            (tenant_id, department_id),
        ).fetchone()
        return row is not None

    def _guard_department_parent(
        self, conn: Any, tenant_id: str, department_id: str, parent_id: str
    ) -> bool:
        """Validate parent visibility, cycle-freedom and depth; ``False`` when hidden."""
        if parent_id == department_id:
            raise WorkBuddyError(ERROR_DEPARTMENT_CYCLE, "a department cannot be its own parent")
        rows = conn.execute(
            "WITH RECURSIVE chain(department_id, parent_department_id, depth) AS ("
            " SELECT department_id, parent_department_id, 1 AS depth FROM workbuddy_departments"
            " WHERE department_id = ? AND tenant_id = ?"
            " UNION ALL"
            " SELECT d.department_id, d.parent_department_id, c.depth + 1 AS depth"
            " FROM workbuddy_departments d JOIN chain c ON d.department_id = c.parent_department_id"
            " WHERE c.depth < 64)"
            " SELECT department_id, depth FROM chain ORDER BY depth DESC",
            (parent_id, tenant_id),
        ).fetchall()
        if not rows:
            return False
        for row in rows:
            if str(row["department_id"]) == department_id:
                raise WorkBuddyError(
                    ERROR_DEPARTMENT_CYCLE, "department hierarchy would contain a cycle"
                )
        limit_row = conn.execute("SELECT workbuddy_department_max_depth() AS max_depth").fetchone()
        max_depth = int(limit_row["max_depth"]) if limit_row else 8
        deepest = max(int(row["depth"]) for row in rows)
        if deepest + 1 > max_depth:
            raise WorkBuddyError(
                ERROR_DEPARTMENT_DEPTH_EXCEEDED,
                f"department hierarchy is limited to {max_depth} levels",
            )
        return True

    def _require_other_owner(self, conn: Any, tenant_id: str, excluded_membership_id: str) -> None:
        row = conn.execute(
            "SELECT count(*) AS owners FROM workbuddy_tenant_members "
            "WHERE tenant_id = ? AND role = 'owner' AND status = 'active' AND membership_id <> ?",
            (tenant_id, excluded_membership_id),
        ).fetchone()
        if not row or int(row["owners"]) == 0:
            raise WorkBuddyError(
                ERROR_LAST_OWNER_REQUIRED, "a tenant must keep at least one active owner"
            )

    def _load_invitation(self, conn: Any, token_hash: str) -> DbRow | None:
        row: DbRow | None = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM workbuddy_invitations WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        return row

    def _load_invitation_row(self, conn: Any, tenant_id: str, invitation_id: str) -> DbRow | None:
        row: DbRow | None = conn.execute(
            f"SELECT {_INVITATION_COLUMNS} FROM workbuddy_invitations "
            "WHERE tenant_id = ? AND invitation_id = ?",
            (tenant_id, invitation_id),
        ).fetchone()
        return row

    def _invitation_usable(self, invitation: DbRow, *, now: int) -> bool:
        if invitation["accepted_at"] is not None or invitation["revoked_at"] is not None:
            return False
        return int(invitation["expires_at"]) > now

    def _require_pending_invitation(self, invitation: DbRow | None, *, now: int) -> None:
        if invitation is None:
            raise WorkBuddyError(ERROR_INVITATION_INVALID, "invitation token is not usable")
        if invitation["accepted_at"] is not None:
            raise WorkBuddyError(
                ERROR_INVITATION_ALREADY_ACCEPTED, "the invitation has already been accepted"
            )
        if invitation["revoked_at"] is not None:
            raise WorkBuddyError(ERROR_INVITATION_REVOKED, "the invitation has been revoked")
        if int(invitation["expires_at"]) <= now:
            raise WorkBuddyError(ERROR_INVITATION_EXPIRED, "the invitation has expired")

    def _require_invitation_email(self, invitation: DbRow, email: object) -> None:
        address = str(email or "").strip().lower()
        if not address or address != str(invitation["email_normalized"]):
            raise WorkBuddyError(
                ERROR_INVITATION_INVALID, "the invitation is bound to a different email address"
            )

    def _available_username(self, conn: Any, email: str, display_name: str | None) -> str:
        local = _USERNAME_SAFE_RE.sub("", email.split("@")[0].strip().lower())
        person = _USERNAME_SAFE_RE.sub("", (display_name or "").strip().lower())
        for candidate in (local, email, person):
            if not candidate or len(candidate) < 3:
                continue
            if self._username_free(conn, candidate):
                return candidate
        for _ in range(5):
            suffix = secrets.token_hex(3)
            candidate = f"{(local or 'user')[:40]}-{suffix}"
            if self._username_free(conn, candidate):
                return candidate
        return f"user-{secrets.token_hex(8)}"

    @staticmethod
    def _username_free(conn: Any, username: str) -> bool:
        row = conn.execute(
            "SELECT 1 AS present FROM users WHERE username = ?", (username,)
        ).fetchone()
        return row is None

    def _seed_quotas(self, conn: Any, tenant_id: str, stamp: int) -> None:
        conn.execute(
            "INSERT INTO workbuddy_tenant_quotas (quota_id, tenant_id, metric, limit_value, created_at, "
            "updated_at) SELECT gen_random_uuid(), ?, metric, default_limit, ?, ? "
            "FROM workbuddy_quota_metrics ON CONFLICT (tenant_id, metric) DO NOTHING",
            (tenant_id, stamp, stamp),
        )

    def _quota_rows(self, conn: Any, tenant_id: str) -> list[DbRow]:
        rows: list[DbRow] = conn.execute(
            "SELECT m.metric AS metric, m.unit AS unit, m.default_limit AS default_limit, "
            "m.hard_cap AS hard_cap, q.quota_id AS quota_id, q.limit_value AS limit_value, "
            "q.updated_by AS updated_by, q.updated_at AS updated_at, "
            "CASE m.metric "
            "WHEN 'users' THEN (SELECT count(*) FROM workbuddy_tenant_members mm "
            "WHERE mm.tenant_id = ? ) "
            "WHEN 'departments' THEN (SELECT count(*) FROM workbuddy_departments dd "
            "WHERE dd.tenant_id = ? AND dd.status <> 'archived') "
            "ELSE NULL END AS used "
            "FROM workbuddy_quota_metrics m "
            "LEFT JOIN workbuddy_tenant_quotas q ON q.metric = m.metric AND q.tenant_id = ? "
            "ORDER BY m.sort_order, m.metric",
            (tenant_id, tenant_id, tenant_id),
        ).fetchall()
        return rows

    def _enforce_user_quota(self, conn: Any, tenant_id: str, *, additional: int = 1) -> None:
        row = self._quota_rows(conn, tenant_id)
        for quota in row:
            if str(quota["metric"]) != "users":
                continue
            limit = (
                int(quota["limit_value"])
                if quota["limit_value"] is not None
                else int(quota["default_limit"])
            )
            hard_cap = int(quota["hard_cap"])
            ceiling = min(limit, hard_cap)
            used = int(quota["used"]) if quota["used"] is not None else 0
            if used + additional > ceiling:
                raise WorkBuddyError(
                    ERROR_QUOTA_EXCEEDED,
                    f"tenant user limit of {ceiling} would be exceeded",
                )
            return

    def _audit(
        self,
        conn: Any,
        tenant_id: str,
        action: str,
        *,
        actor_user_id: int | None,
        reason: str | None,
        detail: str | None,
    ) -> None:
        conn.execute(
            "INSERT INTO workbuddy_tenant_audit_events (event_id, tenant_id, action, actor_user_id, reason, "
            "detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), tenant_id, action, actor_user_id, reason, detail, now_ts()),
        )


def _normalize_limit(value: object, *, metric: str) -> int:
    if isinstance(value, bool) or value is None:
        raise WorkBuddyError(
            ERROR_INVALID_ARGUMENT, f"limit for {metric} must be a non-negative integer"
        )
    if isinstance(value, int):
        limit = value
    else:
        text = str(value).strip()
        if not text.isdigit():
            raise WorkBuddyError(
                ERROR_INVALID_ARGUMENT, f"limit for {metric} must be a non-negative integer"
            )
        limit = int(text)
    if limit < 0:
        raise WorkBuddyError(
            ERROR_INVALID_ARGUMENT, f"limit for {metric} must be a non-negative integer"
        )
    return limit


class _AmbientTransaction:
    """Adapter for joining a caller-owned transaction (``conn=`` keyword)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False

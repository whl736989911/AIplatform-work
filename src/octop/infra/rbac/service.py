"""Tenant-scoped permission operations for any WorkBuddy object.

Callers (work flows, the tool and model catalog, knowledge bases) use this
service instead of the repository directly, so one place decides what a refusal
looks like and one place validates a grant's subject:

* an object the actor cannot read answers ``NOT_FOUND`` — an invisible object and
  a missing one are indistinguishable from outside, which is the rule the
  knowledge base service already follows;
* an object the actor can read but not write answers ``FORBIDDEN``;
* a grant must name a user or department of *this* tenant, and only an actor with
  ``admin`` on the object may hand out or revoke access to it.

Nothing in this module invents a policy beyond those three rules: the four layers
themselves are the scope/ACL model in :mod:`octop.infra.rbac.model`.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.workbuddy_context import WorkBuddyDbContext
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.model import (
    AclGrant,
    Permission,
    RbacActor,
    RbacModelError,
    validate_object_kind,
)
from octop.infra.rbac.repo import (
    WorkBuddyObjectAclRow,
    WorkBuddyObjectScopeRow,
    WorkBuddyRbacRepo,
)
from octop.infra.rbac.resolver import resolve_access


class RbacService:
    """The four-layer permission model, tenant-scoped and fail-closed."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db
        self._repo = WorkBuddyRbacRepo(db)

    # ── registration (the implicit layer) ──────────────────────────────────

    def register_object(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        scope: str,
        actor: RbacActor,
        owner_user_id: int | None = None,
        department_id: str | None = None,
    ) -> dict[str, Any]:
        """Attach the four layers to an object (idempotent).

        The first registration creates the row; every later call moves the
        implicit layer and therefore requires ``admin`` — otherwise anybody who
        can create an object could re-scope somebody else's.
        """
        self._assert_same_tenant(ctx, actor)
        existing = self._repo.get_scope(ctx, object_kind, object_id)
        if existing is not None:
            self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        department = None if department_id in (None, "") else str(department_id)
        if department is not None and not self._repo.department_exists(ctx, department):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "the department does not belong to this tenant",
                details={"path": "department_id"},
            )
        if owner_user_id is not None and not self._repo.member_exists(ctx, int(owner_user_id)):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "the owner is not a member of this tenant",
                details={"path": "owner_user_id"},
            )
        try:
            row = self._repo.set_scope(
                ctx,
                object_kind=object_kind,
                object_id=object_id,
                scope=scope,
                owner_user_id=owner_user_id,
                department_id=department_id,
                created_by_user_id=int(actor.user_id),
            )
        except RbacModelError as exc:
            raise self._invalid(str(exc)) from exc
        return self._scope_view(row)

    def get_scope(
        self, ctx: WorkBuddyDbContext, *, object_kind: str, object_id: str, actor: RbacActor
    ) -> dict[str, Any] | None:
        """The object's implicit layer, visible only to someone who can read it."""
        self._assert_same_tenant(ctx, actor)
        scope_row, _access = self._require_rank(ctx, object_kind, object_id, actor, Permission.READ)
        return None if scope_row is None else self._scope_view(scope_row)

    def unregister_object(
        self, ctx: WorkBuddyDbContext, *, object_kind: str, object_id: str, actor: RbacActor
    ) -> bool:
        """Drop the object's permission rows (its grants cascade away)."""
        self._assert_same_tenant(ctx, actor)
        self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        return self._repo.delete_scope(ctx, object_kind, object_id)

    # ── access ────────────────────────────────────────────────────────────

    def effective_access(
        self, ctx: WorkBuddyDbContext, *, object_kind: str, object_id: str, actor: RbacActor
    ) -> Any:
        """Resolve the actor's permission on one object (no refusal raised)."""
        self._assert_same_tenant(ctx, actor)
        scope_row = self._repo.get_scope(ctx, object_kind, object_id)
        grants = self._repo.effective_grants(
            ctx,
            object_kind,
            object_id,
            user_id=int(actor.user_id),
            department_id=actor.department_id,
        )
        return resolve_access(
            actor,
            None if scope_row is None else scope_row.to_grant(),
            tuple(row.to_grant() for row in grants),
        )

    def require(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        actor: RbacActor,
        permission: str = Permission.READ,
    ) -> Any:
        """Return the resolved access, or refuse like the knowledge base does."""
        scope_row, access = self._require_rank(ctx, object_kind, object_id, actor, permission)
        del scope_row
        return access

    def visible_object_ids(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        actor: RbacActor,
        limit: int = 200,
    ) -> list[str]:
        """The ids of one kind the actor may read, for list queries."""
        self._assert_same_tenant(ctx, actor)
        rows = self._repo.visible_scopes(
            ctx,
            object_kind=object_kind,
            user_id=int(actor.user_id),
            department_id=actor.department_id,
            is_tenant_admin=actor.is_tenant_admin,
            limit=limit,
        )
        return [row.object_id for row in rows]

    # ── grants (the explicit layer) ────────────────────────────────────────

    def list_grants(
        self, ctx: WorkBuddyDbContext, *, object_kind: str, object_id: str, actor: RbacActor
    ) -> list[dict[str, Any]]:
        self._assert_same_tenant(ctx, actor)
        self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        return [
            self._grant_view(row) for row in self._repo.list_grants(ctx, object_kind, object_id)
        ]

    def grant(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        actor: RbacActor,
        permission: str,
        user_id: int | None = None,
        department_id: str | None = None,
    ) -> dict[str, Any]:
        """Grant (or re-grant) one subject the given permission."""
        self._assert_same_tenant(ctx, actor)
        self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        try:
            AclGrant(permission=permission, user_id=user_id, department_id=department_id)
        except RbacModelError as exc:
            raise self._invalid(str(exc)) from exc
        if user_id is not None and not self._repo.member_exists(ctx, int(user_id)):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "the granted user is not a member of this tenant",
                details={"path": "user_id"},
            )
        target_department = None if department_id in (None, "") else str(department_id)
        if target_department is not None and not self._repo.department_exists(
            ctx, target_department
        ):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "the granted department does not belong to this tenant",
                details={"path": "department_id"},
            )
        row = self._repo.upsert_grant(
            ctx,
            object_kind=object_kind,
            object_id=object_id,
            permission=permission,
            user_id=user_id,
            department_id=department_id,
            granted_by_user_id=int(actor.user_id),
        )
        return self._grant_view(row)

    def update_grant(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        acl_id: str,
        actor: RbacActor,
        permission: str,
    ) -> dict[str, Any]:
        """Change a grant's permission; the subject it names never changes."""
        self._assert_same_tenant(ctx, actor)
        self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        if permission not in {member.value for member in Permission}:
            raise self._invalid("permission must be one of: read, write, admin")
        row = self._repo.update_grant_permission(
            ctx,
            object_kind=object_kind,
            object_id=object_id,
            acl_id=acl_id,
            permission=permission,
        )
        if row is None:
            raise OctopError(ErrorCode.NOT_FOUND, "grant not found", details={"path": "acl_id"})
        return self._grant_view(row)

    def revoke_grant(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        acl_id: str,
        actor: RbacActor,
    ) -> dict[str, Any]:
        self._assert_same_tenant(ctx, actor)
        self._require_rank(ctx, object_kind, object_id, actor, Permission.ADMIN)
        revoked = self._repo.delete_grant(ctx, object_kind, object_id, acl_id)
        if not revoked:
            raise OctopError(ErrorCode.NOT_FOUND, "grant not found", details={"path": "acl_id"})
        return {"acl_id": str(acl_id), "revoked": True}

    # ── internals ─────────────────────────────────────────────────────────

    def _require_rank(
        self,
        ctx: WorkBuddyDbContext,
        object_kind: str,
        object_id: str,
        actor: RbacActor,
        permission: str,
    ) -> tuple[WorkBuddyObjectScopeRow | None, Any]:
        try:
            kind = validate_object_kind(object_kind)
        except RbacModelError as exc:
            raise self._invalid(str(exc)) from exc
        scope_row = self._repo.get_scope(ctx, kind, object_id)
        grants = self._repo.effective_grants(
            ctx,
            kind,
            object_id,
            user_id=int(actor.user_id),
            department_id=actor.department_id,
        )
        access = resolve_access(
            actor,
            None if scope_row is None else scope_row.to_grant(),
            tuple(row.to_grant() for row in grants),
        )
        if access.satisfies(permission):
            return scope_row, access
        if not access.can_read:
            # Invisible and missing must look the same from outside.
            raise OctopError(ErrorCode.NOT_FOUND, f"{kind.replace('_', ' ')} not found")
        raise OctopError(
            ErrorCode.FORBIDDEN,
            f"{kind.replace('_', ' ')} requires {permission} permission",
        )

    @staticmethod
    def _assert_same_tenant(ctx: WorkBuddyDbContext, actor: RbacActor) -> None:
        if str(actor.tenant_id) != str(ctx.tenant_id):
            raise OctopError(
                ErrorCode.WORKBUDDY_INVALID_ARGUMENT,
                "the actor does not belong to this tenant",
            )

    @staticmethod
    def _invalid(message: str) -> OctopError:
        return OctopError(ErrorCode.WORKBUDDY_INVALID_ARGUMENT, message)

    @staticmethod
    def _scope_view(row: WorkBuddyObjectScopeRow) -> dict[str, Any]:
        payload = asdict(row)
        return payload

    @staticmethod
    def _grant_view(row: WorkBuddyObjectAclRow) -> dict[str, Any]:
        subject = (
            {"kind": "user", "id": row.user_id}
            if row.user_id is not None
            else {"kind": "department", "id": row.department_id}
        )
        return {
            "acl_id": row.acl_id,
            "object_kind": row.object_kind,
            "object_id": row.object_id,
            "permission": row.permission,
            "subject": subject,
            "granted_by_user_id": row.granted_by_user_id,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


__all__ = ["RbacService"]

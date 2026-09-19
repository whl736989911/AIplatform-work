"""SQL for the generic permission tables (``050_workbuddy_object_rbac``).

Every statement runs inside :func:`octop.infra.db.workbuddy_context.workbuddy_transaction`,
so the tenant comes from the transaction's own GUC and the row-level security of
the two tables is the only tenant boundary — a repository method physically
cannot read another tenant's grants.

Rows are returned as typed views; the resolver's model objects are built by
:mod:`octop.infra.rbac.service`, which keeps this module free of policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.rbac.model import (
    AclGrant,
    RbacActor,
    ScopeGrant,
    validate_object_kind,
    validate_scope_shape,
)
from octop.infra.rbac.resolver import visibility_sql

_SCOPE_COLUMNS = (
    "tenant_id, object_kind, object_id, scope, owner_user_id, department_id, "
    "created_by_user_id, created_at, updated_at"
)
_ACL_COLUMNS = (
    "tenant_id, acl_id, object_kind, object_id, user_id, department_id, permission, "
    "granted_by_user_id, created_at, updated_at"
)


def new_uuid() -> str:
    """A fresh public identifier for a permission row."""
    return str(uuid.uuid4())


def tenant_scoped_id(ctx: WorkBuddyDbContext) -> str:
    """The context's tenant, refusing a context that has none.

    The permission tables exist per tenant and their row-level security reads the
    transaction's tenant, so a platform or context-free call has nothing to act on.
    """
    tenant_id = ctx.tenant_id
    if not tenant_id:
        raise ValueError("the permission tables are tenant scoped; the context has no tenant")
    return str(tenant_id)


@dataclass(frozen=True, slots=True)
class WorkBuddyObjectScopeRow:
    """One ``workbuddy_object_scopes`` row."""

    tenant_id: str
    object_kind: str
    object_id: str
    scope: str
    owner_user_id: int | None
    department_id: str | None
    created_by_user_id: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyObjectScopeRow:
        return cls(
            tenant_id=str(row["tenant_id"]),
            object_kind=str(row["object_kind"]),
            object_id=str(row["object_id"]),
            scope=str(row["scope"]),
            owner_user_id=None if row["owner_user_id"] is None else int(row["owner_user_id"]),
            department_id=None if row["department_id"] is None else str(row["department_id"]),
            created_by_user_id=int(row["created_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def to_grant(self) -> ScopeGrant:
        return ScopeGrant(
            scope=self.scope,
            owner_user_id=self.owner_user_id,
            department_id=self.department_id,
        )


@dataclass(frozen=True, slots=True)
class WorkBuddyObjectAclRow:
    """One ``workbuddy_object_acl`` row."""

    tenant_id: str
    acl_id: str
    object_kind: str
    object_id: str
    user_id: int | None
    department_id: str | None
    permission: str
    granted_by_user_id: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyObjectAclRow:
        return cls(
            tenant_id=str(row["tenant_id"]),
            acl_id=str(row["acl_id"]),
            object_kind=str(row["object_kind"]),
            object_id=str(row["object_id"]),
            user_id=None if row["user_id"] is None else int(row["user_id"]),
            department_id=None if row["department_id"] is None else str(row["department_id"]),
            permission=str(row["permission"]),
            granted_by_user_id=int(row["granted_by_user_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def to_grant(self) -> AclGrant:
        return AclGrant(
            permission=self.permission,
            user_id=self.user_id,
            department_id=self.department_id,
            acl_id=self.acl_id,
        )


class WorkBuddyRbacRepo:
    """Tenant-scoped access to the generic permission tables."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ── scopes (the implicit layer) ────────────────────────────────────────

    def get_scope(
        self, ctx: WorkBuddyDbContext, object_kind: str, object_id: str
    ) -> WorkBuddyObjectScopeRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"SELECT {_SCOPE_COLUMNS} FROM workbuddy_object_scopes "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ?",
                (ctx.tenant_id, validate_object_kind(object_kind), str(object_id)),
            ).fetchone()
        return WorkBuddyObjectScopeRow.from_row(row) if row is not None else None

    def list_scopes(
        self, ctx: WorkBuddyDbContext, object_kind: str, object_ids: Sequence[str]
    ) -> list[WorkBuddyObjectScopeRow]:
        identifiers = [str(value) for value in object_ids]
        if not identifiers:
            return []
        placeholders = ", ".join("?" for _ in identifiers)
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                f"SELECT {_SCOPE_COLUMNS} FROM workbuddy_object_scopes "
                f"WHERE tenant_id = ? AND object_kind = ? AND object_id IN ({placeholders})",
                (ctx.tenant_id, validate_object_kind(object_kind), *identifiers),
            ).fetchall()
        return [WorkBuddyObjectScopeRow.from_row(row) for row in rows]

    def visible_scopes(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        user_id: int,
        department_id: str | None,
        is_tenant_admin: bool = False,
        limit: int = 200,
    ) -> list[WorkBuddyObjectScopeRow]:
        """The objects of one kind the caller may read, most recent first.

        This is :func:`octop.infra.rbac.resolver.visibility_sql` applied to the
        scope table itself, so a listing and a single-object check cannot disagree
        — including the tenant-admin arm, which is why the flag travels with the
        actor and is never defaulted away here.
        """
        tenant_id = tenant_scoped_id(ctx)
        actor = RbacActor(
            user_id=int(user_id),
            tenant_id=tenant_id,
            department_id=department_id,
            is_tenant_admin=is_tenant_admin,
        )
        fragment, params = visibility_sql(actor, scope_table="s", acl_table="a")
        kind = validate_object_kind(object_kind)
        sql = (
            f"SELECT s.object_kind, s.object_id, s.scope, s.owner_user_id, s.department_id, "
            "s.created_at FROM workbuddy_object_scopes s "
            f"WHERE s.tenant_id = ? AND s.object_kind = ? AND {fragment} "
            "ORDER BY s.created_at DESC, s.object_id LIMIT ?"
        )
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(sql, (tenant_id, kind, *params, int(limit))).fetchall()
        return [
            WorkBuddyObjectScopeRow(
                tenant_id=tenant_id,
                object_kind=str(row["object_kind"]),
                object_id=str(row["object_id"]),
                scope=str(row["scope"]),
                owner_user_id=None if row["owner_user_id"] is None else int(row["owner_user_id"]),
                department_id=None if row["department_id"] is None else str(row["department_id"]),
                created_by_user_id=0,
                created_at=int(row["created_at"]),
                updated_at=int(row["created_at"]),
            )
            for row in rows
        ]

    def set_scope(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        scope: str,
        owner_user_id: int | None,
        department_id: str | None,
        created_by_user_id: int,
    ) -> WorkBuddyObjectScopeRow:
        """Register an object, or move its implicit layer; identity never changes."""
        kind = validate_object_kind(object_kind)
        identifier = str(object_id)
        parsed_scope = validate_scope_shape(
            scope, owner_user_id=owner_user_id, department_id=department_id
        )
        owner = None if owner_user_id is None else int(owner_user_id)
        department = None if department_id in (None, "") else str(department_id)
        stamp = now_ts()
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"INSERT INTO workbuddy_object_scopes ({_SCOPE_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (tenant_id, object_kind, object_id) DO UPDATE SET "
                "scope = EXCLUDED.scope, owner_user_id = EXCLUDED.owner_user_id, "
                "department_id = EXCLUDED.department_id, updated_at = EXCLUDED.updated_at "
                f"RETURNING {_SCOPE_COLUMNS}",
                (
                    ctx.tenant_id,
                    kind,
                    identifier,
                    parsed_scope,
                    owner,
                    department,
                    int(created_by_user_id),
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - the statement returns its row
            raise RuntimeError("workbuddy_object_scopes upsert returned no row")
        return WorkBuddyObjectScopeRow.from_row(row)

    def delete_scope(self, ctx: WorkBuddyDbContext, object_kind: str, object_id: str) -> bool:
        """Unregister an object; its grants go with it (ON DELETE CASCADE)."""
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "DELETE FROM workbuddy_object_scopes "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? RETURNING object_id",
                (ctx.tenant_id, validate_object_kind(object_kind), str(object_id)),
            ).fetchone()
        return row is not None

    # ── grants (the explicit layer) ────────────────────────────────────────

    def list_grants(
        self, ctx: WorkBuddyDbContext, object_kind: str, object_id: str
    ) -> list[WorkBuddyObjectAclRow]:
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                f"SELECT {_ACL_COLUMNS} FROM workbuddy_object_acl "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? "
                "ORDER BY created_at, acl_id",
                (ctx.tenant_id, validate_object_kind(object_kind), str(object_id)),
            ).fetchall()
        return [WorkBuddyObjectAclRow.from_row(row) for row in rows]

    def effective_grants(
        self,
        ctx: WorkBuddyDbContext,
        object_kind: str,
        object_id: str,
        *,
        user_id: int,
        department_id: str | None,
    ) -> list[WorkBuddyObjectAclRow]:
        """The grants that address the caller (read on every transaction)."""
        with workbuddy_transaction(self._db, ctx) as conn:
            rows = conn.execute(
                f"SELECT {_ACL_COLUMNS} FROM workbuddy_object_acl "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? "
                "AND (user_id = ? OR department_id = ?)",
                (
                    ctx.tenant_id,
                    validate_object_kind(object_kind),
                    str(object_id),
                    int(user_id),
                    None if department_id in (None, "") else str(department_id),
                ),
            ).fetchall()
        return [WorkBuddyObjectAclRow.from_row(row) for row in rows]

    def get_grant(
        self, ctx: WorkBuddyDbContext, object_kind: str, object_id: str, acl_id: str
    ) -> WorkBuddyObjectAclRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"SELECT {_ACL_COLUMNS} FROM workbuddy_object_acl "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? AND acl_id = ?",
                (
                    ctx.tenant_id,
                    validate_object_kind(object_kind),
                    str(object_id),
                    str(acl_id),
                ),
            ).fetchone()
        return WorkBuddyObjectAclRow.from_row(row) if row is not None else None

    def upsert_grant(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        permission: str,
        user_id: int | None,
        department_id: str | None,
        granted_by_user_id: int,
        acl_id: str | None = None,
    ) -> WorkBuddyObjectAclRow:
        """Grant (or re-grant) one subject; the same subject never has two rows."""
        kind = validate_object_kind(object_kind)
        identifier = str(object_id)
        stamp = now_ts()
        subject_column = "user_id" if user_id is not None else "department_id"
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                f"INSERT INTO workbuddy_object_acl ({_ACL_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                f"ON CONFLICT (tenant_id, object_kind, object_id, {subject_column}) "
                f"WHERE {subject_column} IS NOT NULL "
                "DO UPDATE SET permission = EXCLUDED.permission, updated_at = EXCLUDED.updated_at "
                f"RETURNING {_ACL_COLUMNS}",
                (
                    ctx.tenant_id,
                    acl_id or new_uuid(),
                    kind,
                    identifier,
                    None if user_id is None else int(user_id),
                    None if user_id is not None else str(department_id),
                    str(permission),
                    int(granted_by_user_id),
                    stamp,
                    stamp,
                ),
            ).fetchone()
        if row is None:  # pragma: no cover - the statement returns its row
            raise RuntimeError("workbuddy_object_acl upsert returned no row")
        return WorkBuddyObjectAclRow.from_row(row)

    def update_grant_permission(
        self,
        ctx: WorkBuddyDbContext,
        *,
        object_kind: str,
        object_id: str,
        acl_id: str,
        permission: str,
    ) -> WorkBuddyObjectAclRow | None:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "UPDATE workbuddy_object_acl SET permission = ?, updated_at = ? "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? AND acl_id = ? "
                f"RETURNING {_ACL_COLUMNS}",
                (
                    str(permission),
                    now_ts(),
                    ctx.tenant_id,
                    validate_object_kind(object_kind),
                    str(object_id),
                    str(acl_id),
                ),
            ).fetchone()
        return WorkBuddyObjectAclRow.from_row(row) if row is not None else None

    def delete_grant(
        self, ctx: WorkBuddyDbContext, object_kind: str, object_id: str, acl_id: str
    ) -> bool:
        """Revoke a grant; the next transaction simply does not read the row."""
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "DELETE FROM workbuddy_object_acl "
                "WHERE tenant_id = ? AND object_kind = ? AND object_id = ? AND acl_id = ? "
                "RETURNING acl_id",
                (
                    ctx.tenant_id,
                    validate_object_kind(object_kind),
                    str(object_id),
                    str(acl_id),
                ),
            ).fetchone()
        return row is not None

    # ── subject existence (grants must name a real member or department) ────

    def member_exists(self, ctx: WorkBuddyDbContext, user_id: int) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM workbuddy_tenant_members "
                "WHERE tenant_id = ? AND user_id = ?",
                (ctx.tenant_id, int(user_id)),
            ).fetchone()
        return row is not None

    def department_exists(self, ctx: WorkBuddyDbContext, department_id: str) -> bool:
        with workbuddy_transaction(self._db, ctx) as conn:
            row = conn.execute(
                "SELECT 1 AS found FROM workbuddy_departments "
                "WHERE tenant_id = ? AND department_id = ?",
                (ctx.tenant_id, str(department_id)),
            ).fetchone()
        return row is not None


__all__ = [
    "WorkBuddyObjectAclRow",
    "WorkBuddyObjectScopeRow",
    "WorkBuddyRbacRepo",
    "new_uuid",
    "tenant_scoped_id",
]

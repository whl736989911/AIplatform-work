"""Tenant duties: a responsibility a member holds without becoming an admin.

The tenant role vocabulary (``owner``/``admin``) answers "who runs this tenant".
A duty answers a narrower question: which *job* may this member do? The five
duties below are the jobs the platform gates today, and every one of them was
previously reachable only by promoting a member to tenant admin — which is
exactly what ``B-05`` removes:

* ``author`` — write and save workflow versions;
* ``publisher`` — publish, roll back and archive workflows;
* ``approver`` — decide approvals;
* ``kb_admin`` — govern knowledge bases;
* ``ops`` — operate runs and platform allowances.

A duty row is a subject grant with the same three subject kinds as every other
grant in the platform (see :mod:`octop.infra.rbac.subjects`), so it can be held
by the whole tenant, by a department, or by one member. Tenant admins hold every
duty implicitly: the gates below never take an existing power away, they add a
path for a member who is not an admin.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.rbac.model import RbacActor
from octop.infra.rbac.repo import tenant_scoped_id
from octop.infra.rbac.subjects import (
    SUBJECT_DEPARTMENT_CHAIN,
    SUBJECT_MEMBER,
    SUBJECT_TENANT,
    SubjectError,
    grant_subject_reach,
    resolve_subject,
)

DUTY_AUTHOR = "author"
DUTY_PUBLISHER = "publisher"
DUTY_APPROVER = "approver"
DUTY_KB_ADMIN = "kb_admin"
DUTY_OPS = "ops"
DUTIES: tuple[str, ...] = (
    DUTY_AUTHOR,
    DUTY_PUBLISHER,
    DUTY_APPROVER,
    DUTY_KB_ADMIN,
    DUTY_OPS,
)

DUTY_TABLE = "workbuddy_tenant_duty_grants"

CODE_DUTY_UNKNOWN = "WORKBUDDY_DUTY_UNKNOWN"

__all__ = [
    "CODE_DUTY_UNKNOWN",
    "DUTIES",
    "DUTY_APPROVER",
    "DUTY_AUTHOR",
    "DUTY_KB_ADMIN",
    "DUTY_OPS",
    "DUTY_PUBLISHER",
    "DUTY_TABLE",
    "WorkBuddyDutyError",
    "WorkBuddyDutyGrant",
    "WorkBuddyDutyRepo",
    "actor_holds_duty",
    "duties_for_actor",
    "require_duty",
    "validate_duty",
]


class WorkBuddyDutyError(ValueError):
    """A duty grant that cannot be stored or read back.

    ``code`` is a stable identifier callers map onto their own HTTP status.
    """

    code = CODE_DUTY_UNKNOWN

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        self.message = message
        super().__init__(message)


def validate_duty(value: Any) -> str:
    duty = str(value or "").strip()
    if duty not in DUTIES:
        raise WorkBuddyDutyError(f"duty must be one of {', '.join(DUTIES)}")
    return duty


@dataclass(frozen=True)
class WorkBuddyDutyGrant:
    """One duty held by a tenant, a department, or a member."""

    tenant_id: str
    duty: str
    user_id: int | None
    department_id: str | None
    granted_by_member_id: str | None
    granted_at: int

    @property
    def subject_kind(self) -> str:
        if self.user_id is not None:
            return SUBJECT_MEMBER
        if self.department_id is not None:
            return "department"
        return SUBJECT_TENANT

    @property
    def subject_id(self) -> str | None:
        if self.user_id is not None:
            return str(self.user_id)
        return self.department_id

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyDutyGrant:
        user_id = row["user_id"]
        department_id = row["department_id"]
        return cls(
            tenant_id=str(row["tenant_id"]),
            duty=str(row["duty"]),
            user_id=None if user_id is None else int(user_id),
            department_id=None if department_id is None else str(department_id),
            granted_by_member_id=(
                None
                if row["granted_by_membership_id"] is None
                else str(row["granted_by_membership_id"])
            ),
            granted_at=int(row["granted_at"]),
        )


_DUTY_COLUMNS = (
    "tenant_id, duty, subject_key, user_id, department_id, granted_by_membership_id, granted_at"
)


class WorkBuddyDutyRepo:
    """Duty grants, always inside the caller's WorkBuddy tenant transaction."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @contextmanager
    def _transaction(self, ctx: WorkBuddyDbContext, conn: Any | None = None) -> Iterator[Any]:
        """Own transaction, or the caller's when the duty row must commit with it."""
        if conn is not None:
            yield conn
            return
        with workbuddy_transaction(self._db, ctx) as opened:
            yield opened

    def list_grants(
        self, ctx: WorkBuddyDbContext, *, duty: str | None = None, conn: Any | None = None
    ) -> list[WorkBuddyDutyGrant]:
        tenant_id = tenant_scoped_id(ctx)
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if duty is not None:
            clauses.append("duty = ?")
            params.append(validate_duty(duty))
        with self._transaction(ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM {DUTY_TABLE} WHERE {' AND '.join(clauses)}"
                " ORDER BY duty, subject_key",
                tuple(params),
            ).fetchall()
        return [WorkBuddyDutyGrant.from_row(row) for row in rows]

    def grant(
        self,
        ctx: WorkBuddyDbContext,
        *,
        duty: str,
        subject_kind: str,
        actor_member_id: str,
        subject_id: str | None = None,
        conn: Any | None = None,
    ) -> WorkBuddyDutyGrant:
        """Grant one duty; granting it again to the same subject refreshes the stamp."""
        clean_duty = validate_duty(duty)
        tenant_id = tenant_scoped_id(ctx)
        with self._transaction(ctx, conn) as c:
            try:
                key, user_id, department_id = resolve_subject(
                    c, tenant_id, subject_kind, subject_id
                )
            except SubjectError as exc:
                raise WorkBuddyDutyError(exc.message, code=exc.code) from exc
            row = c.execute(
                f"INSERT INTO {DUTY_TABLE}({_DUTY_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, duty, subject_key) DO UPDATE SET"
                " granted_by_membership_id = EXCLUDED.granted_by_membership_id,"
                " granted_at = EXCLUDED.granted_at"
                " RETURNING *",
                (
                    tenant_id,
                    clean_duty,
                    key,
                    user_id,
                    department_id,
                    actor_member_id,
                    now_ts(),
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyDutyError("duty grant returned no row")
        return WorkBuddyDutyGrant.from_row(row)

    def revoke(
        self,
        ctx: WorkBuddyDbContext,
        *,
        duty: str,
        subject_kind: str,
        subject_id: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        """Drop one subject's duty; ``False`` when nothing matched."""
        clean_duty = validate_duty(duty)
        tenant_id = tenant_scoped_id(ctx)
        with self._transaction(ctx, conn) as c:
            try:
                key, _, _ = resolve_subject(
                    c, tenant_id, subject_kind, subject_id, require_active=False
                )
            except SubjectError as exc:
                raise WorkBuddyDutyError(exc.message, code=exc.code) from exc
            row = c.execute(
                f"DELETE FROM {DUTY_TABLE} WHERE tenant_id = ? AND duty = ? AND subject_key = ?"
                " RETURNING subject_key",
                (tenant_id, clean_duty, key),
            ).fetchone()
        return row is not None


def duties_for_actor(
    db: DatabasePool, actor: RbacActor, *, conn: Any | None = None
) -> frozenset[str]:
    """Every duty reaching this actor: tenant-wide, department, or personal.

    A tenant admin holds all of them — the gate must never take an existing
    power away from the people who run the tenant.
    """
    if actor.is_tenant_admin:
        return frozenset(DUTIES)
    ctx = WorkBuddyDbContext.for_tenant(actor.tenant_id, user_id=actor.user_id)
    with workbuddy_transaction(db, ctx) as c:
        rows = c.execute(
            SUBJECT_DEPARTMENT_CHAIN + " "
            f"SELECT DISTINCT g.duty FROM {DUTY_TABLE} g"
            " WHERE g.tenant_id = ? AND " + grant_subject_reach("g"),
            (
                actor.tenant_id,
                actor.user_id,
                actor.tenant_id,
                actor.tenant_id,
                actor.user_id,
            ),
        ).fetchall()
    return frozenset(str(row["duty"]) for row in rows)


def actor_holds_duty(db: DatabasePool, actor: RbacActor, duty: str) -> bool:
    """True when the actor may do this duty's job."""
    clean_duty = validate_duty(duty)
    return clean_duty in duties_for_actor(db, actor)


def require_duty(db: DatabasePool, actor: RbacActor, duty: str) -> None:
    """Refuse the caller unless they hold the duty (admins always do)."""
    clean_duty = validate_duty(duty)
    if actor_holds_duty(db, actor, clean_duty):
        return
    raise OctopError(
        ErrorCode.FORBIDDEN,
        f"{clean_duty} duty required",
        details={"duty": clean_duty, "role": "member"},
    )

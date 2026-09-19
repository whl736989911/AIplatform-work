"""WorkBuddy tenancy database context and the PostgreSQL-only transaction boundary.

WorkBuddy keeps tenant data in PostgreSQL with row level security (RLS) keyed on
the transaction-local ``app.*`` settings written here:

* ``app.tenant_id``     — public UUID of the active tenant (empty when absent)
* ``app.user_id``       — octop ``users.id`` of the acting user (empty when absent)
* ``app.department_id`` — public UUID of the acting department (empty when absent)
* ``app.system``        — ``on`` for platform-wide (cross-tenant) work, else ``off``

Every value is applied with ``set_config(name, value, is_local => true)`` inside the
transaction, so a checked-in connection can never inherit a tenant from the previous
request: absent values are explicitly cleared, and all four settings disappear when
the transaction commits or rolls back.

SQLite installs keep the no-op schema marker ``015_workbuddy_identity.sql`` and this
module fails closed with :class:`WorkBuddyPostgresRequiredError` (stable code
``DEPENDENCY_UNAVAILABLE``) instead of serving tenant data without isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool

GUC_TENANT_ID = "app.tenant_id"
GUC_USER_ID = "app.user_id"
GUC_DEPARTMENT_ID = "app.department_id"
GUC_SYSTEM = "app.system"

POSTGRES_DIALECT = "postgresql"
SYSTEM_ON = "on"
SYSTEM_OFF = "off"


class WorkBuddyPostgresRequiredError(RuntimeError):
    """WorkBuddy was asked to run on a non-PostgreSQL database (fail closed)."""

    code = "DEPENDENCY_UNAVAILABLE"
    message = "WorkBuddy requires a PostgreSQL database"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)


class WorkBuddyContextError(ValueError):
    """The tenant context is malformed (never contains secrets)."""

    code = "WORKBUDDY_CONTEXT_INVALID"
    message = "WorkBuddy database context is invalid"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)


def normalize_uuid(value: object, *, field: str = "id") -> str:
    """Canonical lowercase UUID string for a public WorkBuddy identifier."""
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value).strip()))
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkBuddyContextError(f"{field} must be a UUID string") from exc


def reject_none_uuid(value: object, *, field: str = "id") -> str | None:
    """Normalize an optional public identifier; empty means absent."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return normalize_uuid(value, field=field)


def normalize_user_id(value: object, *, field: str = "user_id") -> int:
    """Positive integer octop ``users.id``."""
    if isinstance(value, bool) or value is None:
        raise WorkBuddyContextError(f"{field} must be a positive integer")
    if isinstance(value, int):
        user_id = value
    else:
        text = str(value).strip()
        if not text.isdigit():
            raise WorkBuddyContextError(f"{field} must be a positive integer")
        user_id = int(text)
    if user_id <= 0:
        raise WorkBuddyContextError(f"{field} must be a positive integer")
    return user_id


def reject_none_user_id(value: object, *, field: str = "user_id") -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return normalize_user_id(value, field=field)


@dataclass(frozen=True)
class WorkBuddyDbContext:
    """Tenant/user/department scope applied to one WorkBuddy transaction."""

    tenant_id: str | None = None
    user_id: int | None = None
    department_id: str | None = None
    system: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", reject_none_uuid(self.tenant_id, field="tenant_id"))
        object.__setattr__(self, "user_id", reject_none_user_id(self.user_id, field="user_id"))
        object.__setattr__(
            self, "department_id", reject_none_uuid(self.department_id, field="department_id")
        )
        object.__setattr__(self, "system", bool(self.system))

    @classmethod
    def platform(cls, *, tenant_id: object = None, user_id: object = None) -> WorkBuddyDbContext:
        """Cross-tenant (platform operator) context; optional tenant/user stamps."""
        return cls(
            tenant_id=reject_none_uuid(tenant_id, field="tenant_id"),
            user_id=reject_none_user_id(user_id),
            system=True,
        )

    @classmethod
    def for_tenant(
        cls,
        tenant_id: object,
        *,
        user_id: object = None,
        department_id: object = None,
    ) -> WorkBuddyDbContext:
        """Single-tenant context: RLS restricts every statement to this tenant."""
        return cls(
            tenant_id=normalize_uuid(tenant_id, field="tenant_id"),
            user_id=reject_none_user_id(user_id),
            department_id=reject_none_uuid(department_id, field="department_id"),
            system=False,
        )

    def gucs(self) -> dict[str, str]:
        """Transaction-local settings for this context (absent values cleared)."""
        return {
            GUC_TENANT_ID: self.tenant_id or "",
            GUC_USER_ID: "" if self.user_id is None else str(self.user_id),
            GUC_DEPARTMENT_ID: self.department_id or "",
            GUC_SYSTEM: SYSTEM_ON if self.system else SYSTEM_OFF,
        }


def _pick(source: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if source.get(name) is not None:
            return source.get(name)
    return None


def coerce_workbuddy_context(value: Any) -> WorkBuddyDbContext:
    """Accept a context, a mapping, a principal-like object, or ``None`` (fail closed).

    ``None`` is deliberately an empty context (no tenant, no system flag) so a missing
    context can never escalate to platform access.
    """
    if value is None:
        return WorkBuddyDbContext()
    if isinstance(value, WorkBuddyDbContext):
        return value
    if isinstance(value, Mapping):
        return WorkBuddyDbContext(
            tenant_id=_pick(value, "tenant_id"),
            user_id=_pick(value, "user_id", "octop_user_id"),
            department_id=_pick(value, "department_id"),
            system=bool(value.get("system", False)),
        )
    return WorkBuddyDbContext(
        tenant_id=getattr(value, "tenant_id", None),
        user_id=getattr(value, "user_id", None) or getattr(value, "octop_user_id", None),
        department_id=getattr(value, "department_id", None),
        system=bool(getattr(value, "system", False)),
    )


def require_postgres(db: Any) -> None:
    """Raise the controlled WorkBuddy error unless ``db`` is a PostgreSQL pool."""
    dialect = getattr(db, "dialect", None)
    if dialect != POSTGRES_DIALECT:
        raise WorkBuddyPostgresRequiredError(
            "WorkBuddy requires a PostgreSQL database; "
            f"the configured backend is {dialect if dialect else 'unknown'}"
        )


def apply_workbuddy_context(conn: Any, ctx: Any = None) -> WorkBuddyDbContext:
    """Write the context settings transaction-locally on ``conn``.

    Must be called inside an open transaction; ``is_local => true`` makes PostgreSQL
    discard every setting at commit or rollback.
    """
    context = coerce_workbuddy_context(ctx)
    for name, value in context.gucs().items():
        conn.execute("SELECT set_config(?, ?, true)", (name, value))
    return context


def current_workbuddy_context(conn: Any) -> WorkBuddyDbContext:
    """Read the settings currently visible on ``conn`` (used by tests and diagnostics).

    Malformed session settings (never written by this module) read as an empty,
    fail-closed context instead of raising.
    """
    row = conn.execute(
        "SELECT current_setting(?, true) AS tenant_id, current_setting(?, true) AS user_id, "
        "current_setting(?, true) AS department_id, current_setting(?, true) AS system",
        (GUC_TENANT_ID, GUC_USER_ID, GUC_DEPARTMENT_ID, GUC_SYSTEM),
    ).fetchone()

    def text(name: str) -> str | None:
        if row is None:
            return None
        value = row[name]
        if value is None:
            return None
        stripped = str(value).strip()
        return stripped or None

    try:
        return WorkBuddyDbContext(
            tenant_id=text("tenant_id"),
            user_id=reject_none_user_id(text("user_id")),
            department_id=text("department_id"),
            system=(text("system") or SYSTEM_OFF) == SYSTEM_ON,
        )
    except WorkBuddyContextError:
        return WorkBuddyDbContext()


@contextmanager
def workbuddy_transaction(db: DatabasePool, ctx: Any = None) -> Iterator[Any]:
    """Open a WorkBuddy transaction with the tenant context applied.

    Yields the connection (sqlite-like ``execute(sql, params)`` with ``?``
    placeholders, commit on clean exit, rollback on exception). Raises
    :class:`WorkBuddyPostgresRequiredError` for non-PostgreSQL pools before any
    connection is checked out.

    The settings are written with ``is_local => true``, so PostgreSQL drops them at
    commit or rollback, and the context that was in force when the block was entered
    is restored before the block ends. A tenant scope therefore cannot outlive its
    ``with`` block, not even when the pool hands out a connection that is already
    inside a caller-owned transaction.
    """
    require_postgres(db)
    context = coerce_workbuddy_context(ctx)
    with db.transaction() as conn:
        previous = current_workbuddy_context(conn)
        apply_workbuddy_context(conn, context)
        try:
            yield conn
        except BaseException:
            # The transaction rolls back, which discards every local setting; touching
            # the connection here would raise InFailedSqlTransaction and mask the cause.
            raise
        apply_workbuddy_context(conn, previous)

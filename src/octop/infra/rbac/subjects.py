"""Who a tenant-scoped grant is for.

WorkBuddy grants — capability allowances (tools and models), tenant duties, and
anything added later — all answer the same question: which members does this row
reach? The answer is one of three subject kinds, and this module is the only
place that spells them out:

* ``tenant`` — every active member of the tenant (no subject columns);
* ``department`` — the members of that department and of its sub-departments;
* ``member`` — exactly one user.

``subject_key`` materialises the subject into a text key so it can sit in a
primary key next to the object revision or duty name; the ``user_id`` and
``department_id`` columns stay behind for referential integrity. Callers that
store rows with these columns must check the same shape the database enforces
(see the ``*_subject_shape`` constraints).
"""

from __future__ import annotations

from typing import Any

SUBJECT_TENANT = "tenant"
SUBJECT_DEPARTMENT = "department"
SUBJECT_MEMBER = "member"
SUBJECT_KINDS = (SUBJECT_TENANT, SUBJECT_DEPARTMENT, SUBJECT_MEMBER)

# ``tenant`` / ``department:<uuid>`` / ``member:<user id>``.
SUBJECT_KEY_SEPARATOR = ":"

# The members of one department and of its sub-departments, for a caller whose
# department grant must also reach them. Parameters: (tenant, user, tenant).
SUBJECT_DEPARTMENT_CHAIN = (
    "WITH RECURSIVE wb_subject_departments(department_id) AS ("
    " SELECT m.department_id FROM workbuddy_tenant_members m"
    " WHERE m.tenant_id = ? AND m.user_id = ? AND m.department_id IS NOT NULL"
    " UNION"
    " SELECT d.parent_department_id FROM workbuddy_departments d"
    " JOIN wb_subject_departments c ON d.department_id = c.department_id"
    " WHERE d.tenant_id = ? AND d.parent_department_id IS NOT NULL)"
)

CODE_SUBJECT_UNKNOWN = "WORKBUDDY_GRANT_SUBJECT_UNKNOWN"


class SubjectError(ValueError):
    """A subject that cannot be stored: unknown kind, missing id, foreign tenant.

    ``code`` is the stable identifier callers map onto their own error type.
    """

    code = CODE_SUBJECT_UNKNOWN

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        self.message = message
        super().__init__(message)


def subject_key(subject_kind: str, identity: str | int | None) -> str:
    """The materialised key of one subject.

    ``identity`` is the department id or the user id; a tenant-wide subject
    carries neither.
    """
    if subject_kind == SUBJECT_TENANT:
        if identity not in (None, ""):
            raise SubjectError("a tenant-wide grant carries no subject id")
        return SUBJECT_TENANT
    if subject_kind == SUBJECT_DEPARTMENT:
        department_id = _required_identity(subject_kind, identity)
        return f"{SUBJECT_DEPARTMENT}{SUBJECT_KEY_SEPARATOR}{department_id}"
    if subject_kind == SUBJECT_MEMBER:
        user_id = _required_identity(subject_kind, identity)
        if not str(user_id).isdigit():
            raise SubjectError("a member grant is addressed by user id")
        return f"{SUBJECT_MEMBER}{SUBJECT_KEY_SEPARATOR}{user_id}"
    raise SubjectError(f"subject_kind must be one of {', '.join(SUBJECT_KINDS)}")


def _required_identity(subject_kind: str, identity: str | int | None) -> str:
    value = str(identity or "").strip()
    if not value:
        raise SubjectError(f"a {subject_kind} grant needs a subject id")
    return value


def grant_subject_reach(alias: str = "g") -> str:
    """Predicate over grant row ``alias``: does its subject reach the calling user?

    The caller binds the user id for the placeholder and supplies the
    :data:`SUBJECT_DEPARTMENT_CHAIN` CTE, so a department grant also reaches the
    members of that department's sub-departments.
    """
    return (
        f"({alias}.subject_key = 'tenant'"
        f" OR ({alias}.user_id IS NOT NULL AND {alias}.user_id = ?)"
        f" OR ({alias}.department_id IS NOT NULL AND {alias}.department_id IN"
        " (SELECT department_id FROM wb_subject_departments)))"
    )


def resolve_subject(
    conn: Any,
    tenant_id: str,
    subject_kind: str,
    subject_id: str | int | None,
    *,
    department_table: str = "workbuddy_departments",
    member_table: str = "workbuddy_tenant_members",
    require_active: bool = True,
) -> tuple[str, int | None, str | None]:
    """Map an API subject onto row columns: ``(subject_key, user_id, department_id)``.

    A department must belong to the tenant and a member must be an active member
    of it (unless ``require_active`` is false, which revocation uses so a grant
    can still be dropped after the member left).
    """
    if subject_kind == SUBJECT_TENANT:
        return subject_key(subject_kind, subject_id), None, None
    if subject_kind == SUBJECT_DEPARTMENT:
        department_id = _required_identity(subject_kind, subject_id)
        row = conn.execute(
            f"SELECT 1 FROM {department_table} WHERE tenant_id = ? AND department_id = ?",
            (tenant_id, department_id),
        ).fetchone()
        if row is None:
            raise SubjectError("department does not belong to this tenant")
        return subject_key(subject_kind, department_id), None, department_id
    if subject_kind == SUBJECT_MEMBER:
        user_id = int(_required_identity(subject_kind, subject_id))
        row = conn.execute(
            f"SELECT 1 FROM {member_table} WHERE tenant_id = ? AND user_id = ? AND status = 'active'",
            (tenant_id, user_id),
        ).fetchone()
        if row is None and require_active:
            raise SubjectError("granted user is not an active member of this tenant")
        if row is None:
            row = conn.execute(
                f"SELECT 1 FROM {member_table} WHERE tenant_id = ? AND user_id = ?",
                (tenant_id, user_id),
            ).fetchone()
        if row is None:
            raise SubjectError("user is not a member of this tenant")
        return subject_key(subject_kind, user_id), user_id, None
    raise SubjectError(f"subject_kind must be one of {', '.join(SUBJECT_KINDS)}")


__all__ = [
    "CODE_SUBJECT_UNKNOWN",
    "SUBJECT_DEPARTMENT",
    "SUBJECT_DEPARTMENT_CHAIN",
    "SUBJECT_KEY_SEPARATOR",
    "SUBJECT_KINDS",
    "SUBJECT_MEMBER",
    "SUBJECT_TENANT",
    "SubjectError",
    "grant_subject_reach",
    "resolve_subject",
    "subject_key",
]

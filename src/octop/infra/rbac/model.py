"""Vocabulary of the four-layer permission model.

The knowledge base slice already models the product's four permission layers —
company / department / personal plus explicit grants — as a ``scope`` column with
a shape constraint and additive ``acl`` rows (``migrations/019_*``,
``infra/workbuddy/knowledge.py``).  These types are that model, generalised:

* :class:`Scope` is the implicit layer every permission-bearing object carries.
* :class:`AclGrant` is the explicit layer, additive to the implicit rank.
* :class:`Permission` is the vocabulary of both, ordered by
  :data:`DEFAULT_PERMISSION_RANK` so "read < write < admin" has one definition.

Nothing here is tenant- or database-aware: the values only have to agree with the
CHECK constraints in ``050_workbuddy_object_rbac.pg.sql``, which is what the
databases enforce.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RbacModelError(ValueError):
    """A permission value that the model (and the database) would refuse."""


class Permission(StrEnum):
    """What a caller may do with an object, ordered from least to most."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class Scope(StrEnum):
    """The implicit layer: who may reach the object without an explicit grant."""

    PERSONAL = "personal"
    DEPARTMENT = "department"
    ENTERPRISE = "enterprise"


class AclSubjectKind(StrEnum):
    """Who an explicit grant is for."""

    USER = "user"
    DEPARTMENT = "department"


class ObjectKind(StrEnum):
    """The object families that carry permissions today.

    The database accepts any well-formed kind so a new family does not need a
    migration; this registry is what callers should use, and
    :data:`KNOWN_OBJECT_KINDS` is the list the platform documents.
    """

    WORKFLOW = "workflow"
    KNOWLEDGE_BASE = "knowledge_base"
    TOOL = "tool"
    MODEL = "model"
    CONNECTOR = "connector"
    AGENT = "agent"


#: ``read < write < admin`` — one definition, shared by the resolver and the API.
PERMISSION_ORDER: tuple[str, ...] = (Permission.READ, Permission.WRITE, Permission.ADMIN)
DEFAULT_PERMISSION_RANK: Mapping[str, int] = {
    Permission.READ: 1,
    Permission.WRITE: 2,
    Permission.ADMIN: 3,
}

#: Human-facing registry; the database is deliberately more permissive.
KNOWN_OBJECT_KINDS: frozenset[str] = frozenset(kind.value for kind in ObjectKind)

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def validate_object_kind(value: Any) -> str:
    """Return the normalised kind, or refuse an unusable one."""
    kind = str(value or "").strip()
    if not _KIND_PATTERN.match(kind):
        raise RbacModelError("object_kind must be lower-case words separated by underscores")
    return kind


def parse_permission(value: Any) -> str:
    permission = str(value or "").strip().lower()
    if permission not in DEFAULT_PERMISSION_RANK:
        raise RbacModelError("permission must be one of: read, write, admin")
    return permission


def parse_scope(value: Any) -> str:
    scope = str(value or "").strip().lower()
    if scope not in {member.value for member in Scope}:
        raise RbacModelError("scope must be one of: personal, department, enterprise")
    return scope


def validate_scope_shape(
    scope: Any,
    *,
    owner_user_id: int | None,
    department_id: str | None,
) -> str:
    """Enforce the same shape the database's CHECK constraint enforces.

    A scope names exactly the subject it implies: personal names its owner,
    department names its department, enterprise names neither.
    """
    parsed = parse_scope(scope)
    owner = None if owner_user_id is None else int(owner_user_id)
    department = None if department_id in (None, "") else str(department_id)
    if parsed == Scope.PERSONAL and (owner is None or department is not None):
        raise RbacModelError("a personal scope needs an owner and no department")
    if parsed == Scope.DEPARTMENT and (department is None or owner is not None):
        raise RbacModelError("a department scope needs a department and no owner")
    if parsed == Scope.ENTERPRISE and (owner is not None or department is not None):
        raise RbacModelError("an enterprise scope names neither an owner nor a department")
    return parsed


@dataclass(frozen=True, slots=True)
class RbacActor:
    """Who is asking.

    ``is_tenant_admin`` is the tenant-scoped administration flag, not the Octop
    platform role: it grants the admin rank on department and enterprise objects
    and, exactly like the knowledge base model, nothing on somebody's personal
    object.
    """

    user_id: int
    tenant_id: str
    department_id: str | None = None
    is_tenant_admin: bool = False

    @property
    def subject_id(self) -> int:
        return int(self.user_id)


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    """One object's implicit layer (``workbuddy_object_scopes`` row)."""

    scope: str
    owner_user_id: int | None = None
    department_id: str | None = None

    def __post_init__(self) -> None:
        validate_scope_shape(
            self.scope, owner_user_id=self.owner_user_id, department_id=self.department_id
        )


@dataclass(frozen=True, slots=True)
class AclGrant:
    """One explicit grant (``workbuddy_object_acl`` row).

    A grant names one user *or* one department; both is ambiguous, neither is
    meaningless.
    """

    permission: str
    user_id: int | None = None
    department_id: str | None = None
    acl_id: str | None = None

    def __post_init__(self) -> None:
        parse_permission(self.permission)
        subjects = [self.user_id is not None, self.department_id not in (None, "")]
        if subjects[0] == subjects[1]:
            raise RbacModelError("a grant names exactly one subject: a user or a department")

    @property
    def subject_kind(self) -> AclSubjectKind:
        return AclSubjectKind.USER if self.user_id is not None else AclSubjectKind.DEPARTMENT

    @property
    def subject_id(self) -> str:
        return str(self.user_id) if self.user_id is not None else str(self.department_id)

    def matches(self, actor: RbacActor) -> bool:
        """True when this grant is addressed to the actor or to its department."""
        if self.user_id is not None:
            return int(self.user_id) == int(actor.user_id)
        actor_department = actor.department_id
        return actor_department is not None and str(self.department_id) == str(actor_department)


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """The result of resolving an object's access for one actor."""

    permission: str | None
    rank: int
    sources: tuple[str, ...] = field(default=())

    @property
    def can_read(self) -> bool:
        return self.rank >= DEFAULT_PERMISSION_RANK[Permission.READ]

    @property
    def can_write(self) -> bool:
        return self.rank >= DEFAULT_PERMISSION_RANK[Permission.WRITE]

    @property
    def can_admin(self) -> bool:
        return self.rank >= DEFAULT_PERMISSION_RANK[Permission.ADMIN]

    def satisfies(self, needed: Any) -> bool:
        """True when the decision reaches ``needed`` (read/write/admin)."""
        return self.rank >= DEFAULT_PERMISSION_RANK[parse_permission(needed)]


def grants_from_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[AclGrant, ...]:
    """Build grants from ``workbuddy_object_acl`` rows, ignoring unusable ones.

    A row that cannot be represented as a grant (both subjects, or an unknown
    permission) is refused rather than silently skipped, because a permission row
    the resolver cannot read is a permission nobody can audit.
    """
    grants: list[AclGrant] = []
    for row in rows:
        grants.append(
            AclGrant(
                permission=str(row.get("permission") or ""),
                user_id=None if row.get("user_id") is None else int(row["user_id"]),
                department_id=(
                    None if row.get("department_id") in (None, "") else str(row["department_id"])
                ),
                acl_id=None if row.get("acl_id") is None else str(row["acl_id"]),
            )
        )
    return tuple(grants)

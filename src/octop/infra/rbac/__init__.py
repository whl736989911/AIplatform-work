"""The four-layer permission model shared by every WorkBuddy object.

The product requires four layers — company / department / personal / explicit
grant — and the knowledge bases already implement exactly that shape.  This
package is that model, factored out of the knowledge base so any object can carry
it: an implicit ``scope`` per object (the first three layers) plus additive
``ACL`` rows (the fourth).

Nothing here talks to a database or to HTTP: :mod:`octop.infra.rbac.model` holds
the vocabulary, :mod:`octop.infra.rbac.resolver` the pure rank logic,
:mod:`octop.infra.rbac.repo` the SQL and :mod:`octop.infra.rbac.service` the
tenant-scoped operations callers use.
"""

from __future__ import annotations

from octop.infra.rbac.model import (
    DEFAULT_PERMISSION_RANK,
    KNOWN_OBJECT_KINDS,
    PERMISSION_ORDER,
    AccessDecision,
    AclGrant,
    AclSubjectKind,
    ObjectKind,
    Permission,
    RbacActor,
    Scope,
    ScopeGrant,
    validate_object_kind,
    validate_scope_shape,
)
from octop.infra.rbac.resolver import resolve_access, visibility_sql
from octop.infra.rbac.service import RbacService

__all__ = [
    "DEFAULT_PERMISSION_RANK",
    "KNOWN_OBJECT_KINDS",
    "PERMISSION_ORDER",
    "AccessDecision",
    "AclGrant",
    "AclSubjectKind",
    "ObjectKind",
    "Permission",
    "RbacActor",
    "RbacService",
    "Scope",
    "ScopeGrant",
    "resolve_access",
    "validate_object_kind",
    "validate_scope_shape",
    "visibility_sql",
]

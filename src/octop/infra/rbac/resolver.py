"""Pure permission resolution: implicit scope rank, then additive ACL rank.

The rule is the knowledge base's, verbatim (``infra/workbuddy/knowledge.py``
``resolve_knowledge_access``), because two implementations of "who may see this"
that disagree are worse than one:

* the implicit layer contributes a rank from the object's scope — the owner of a
  personal object is admin, a department's current members read, every active
  tenant member reads an enterprise object, and a tenant admin is admin on
  department and enterprise objects but has *no* implicit access to somebody's
  personal object;
* every matching grant contributes its own rank and the result is the maximum, so
  a grant can add permission and can never take it away;
* no contribution means no permission at all, and callers answer "not found"
  rather than "forbidden" so an invisible object is indistinguishable from a
  missing one.

:func:`visibility_sql` is the same rule expressed as a WHERE fragment for list
queries, so a listing cannot drift from the single-object check.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from octop.infra.rbac.model import (
    DEFAULT_PERMISSION_RANK,
    PERMISSION_ORDER,
    AccessDecision,
    AclGrant,
    RbacActor,
    Scope,
    ScopeGrant,
    validate_object_kind,
)


def _rank_name(rank: int) -> str | None:
    """The strongest permission a rank reaches."""
    best: str | None = None
    for name in PERMISSION_ORDER:
        if rank >= DEFAULT_PERMISSION_RANK[name]:
            best = name
    return best


def resolve_access(
    actor: RbacActor,
    scope: ScopeGrant | None,
    grants: Sequence[AclGrant] = (),
) -> AccessDecision:
    """Resolve one object's access for one actor.

    ``scope`` is ``None`` when the object has no permission row at all: an object
    the permission model has never seen is invisible to everyone, which is the
    fail-closed direction (a new object family that forgets to register itself
    leaks nothing).
    """
    rank = 0
    sources: list[str] = []

    if scope is not None:
        if scope.scope == Scope.PERSONAL:
            if scope.owner_user_id is not None and int(scope.owner_user_id) == int(actor.user_id):
                rank = DEFAULT_PERMISSION_RANK["admin"]
                sources.append("owner")
        elif scope.scope == Scope.DEPARTMENT:
            if (
                actor.department_id is not None
                and scope.department_id is not None
                and str(scope.department_id) == str(actor.department_id)
            ):
                rank = max(rank, DEFAULT_PERMISSION_RANK["read"])
                sources.append("department-member")
            if actor.is_tenant_admin:
                rank = max(rank, DEFAULT_PERMISSION_RANK["admin"])
                sources.append("tenant-admin")
        elif scope.scope == Scope.ENTERPRISE:
            rank = max(rank, DEFAULT_PERMISSION_RANK["read"])
            sources.append("enterprise-member")
            if actor.is_tenant_admin:
                rank = max(rank, DEFAULT_PERMISSION_RANK["admin"])
                sources.append("tenant-admin")

    for grant in grants:
        if not grant.matches(actor):
            continue
        grant_rank = DEFAULT_PERMISSION_RANK.get(grant.permission, 0)
        if grant_rank:
            rank = max(rank, grant_rank)
            sources.append(f"acl:{grant.permission}")

    return AccessDecision(_rank_name(rank), rank, tuple(sources))


def visibility_sql(
    actor: RbacActor,
    *,
    scope_table: str = "s",
    acl_table: str = "a",
    acl_relation: str = "workbuddy_object_acl",
) -> tuple[str, list[Any]]:
    """A WHERE fragment selecting the objects ``actor`` may read, plus its params.

    ``scope_table`` must be a ``workbuddy_object_scopes`` row (or an alias of one)
    joined by the caller; the fragment reads its ``tenant_id``, ``object_kind``
    and ``object_id`` columns, so the caller keeps ownership of the surrounding
    query and its ordering.

    The department arms are only emitted when the actor actually has a
    department: a parameter that is merely compared to NULL has no type for
    PostgreSQL to infer, and a detached member can never match a department grant
    anyway.
    """
    user_id = int(actor.user_id)
    department_id = actor.department_id
    scope_arms = [
        f"{scope_table}.scope = 'enterprise'",
        f"({scope_table}.scope = 'personal' AND {scope_table}.owner_user_id = ?)",
    ]
    grant_arms = [f"{acl_table}.user_id = ?"]
    params: list[Any] = [user_id, user_id]
    if actor.is_tenant_admin:
        # The resolver gives a tenant admin the admin rank on department and
        # enterprise objects, so the filter must not require department membership
        # for them.  Personal objects stay private, exactly like the resolver.
        scope_arms.append(f"{scope_table}.scope = 'department'")
    if department_id is not None:
        scope_arms.append(
            f"({scope_table}.scope = 'department' AND {scope_table}.department_id = ?)"
        )
        grant_arms.append(f"{acl_table}.department_id = ?")
        params = [user_id, department_id, user_id, department_id]
    fragment = (
        f"({' OR '.join(scope_arms)}"
        f" OR EXISTS (SELECT 1 FROM {acl_relation} {acl_table}"
        f" WHERE {acl_table}.tenant_id = {scope_table}.tenant_id"
        f" AND {acl_table}.object_kind = {scope_table}.object_kind"
        f" AND {acl_table}.object_id = {scope_table}.object_id"
        f" AND ({' OR '.join(grant_arms)})))"
    )
    return fragment, params


def visible_kinds_sql(*, scope_table: str = "s", kinds: Sequence[str] | None = None) -> str:
    """Restrict a visibility query to a set of object kinds, in bind order."""
    if not kinds:
        return ""
    placeholders = ", ".join("?" for _ in kinds)
    return f" AND {scope_table}.object_kind IN ({placeholders})"


def validate_visibility_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    """Normalise the kinds a caller passes to a visibility query."""
    return tuple(validate_object_kind(kind) for kind in kinds)

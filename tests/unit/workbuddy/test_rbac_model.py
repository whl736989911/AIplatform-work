"""The four-layer permission model and its resolver, without a database.

The knowledge base already enforces this contract, so the point of these checks
is that the generic model agrees with it exactly — two implementations of "who
may see this" that disagree are worse than one:

* company (``enterprise``), department, personal and explicit grant resolve to one
  rank, and the rank is the *maximum* of what the implicit layer and the matching
  grants allow, so a grant can never take permission away;
* an object nobody registered is invisible to everybody;
* the shape rules the database enforces are enforced here too, so a bad row is
  refused with a readable message instead of a constraint name.
"""

from __future__ import annotations

import pytest

from octop.infra.rbac.model import (
    DEFAULT_PERMISSION_RANK,
    AclGrant,
    AclSubjectKind,
    Permission,
    RbacActor,
    RbacModelError,
    ScopeGrant,
    parse_permission,
    validate_object_kind,
    validate_scope_shape,
)
from octop.infra.rbac.resolver import resolve_access, visibility_sql

OWNER = 11
MEMBER = 22
STRANGER = 33
DEPARTMENT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER_DEPARTMENT = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


def actor(
    user_id: int = MEMBER,
    *,
    department_id: str | None = DEPARTMENT,
    is_tenant_admin: bool = False,
) -> RbacActor:
    return RbacActor(
        user_id=user_id,
        tenant_id="11111111-1111-4111-8111-111111111111",
        department_id=department_id,
        is_tenant_admin=is_tenant_admin,
    )


def test_enterprise_scope_reads_for_every_member() -> None:
    decision = resolve_access(actor(STRANGER), ScopeGrant(scope="enterprise"))
    assert decision.can_read
    assert not decision.can_write
    assert decision.sources == ("enterprise-member",)


def test_personal_scope_belongs_to_its_owner_alone() -> None:
    personal = ScopeGrant(scope="personal", owner_user_id=OWNER)
    owner = resolve_access(actor(OWNER), personal)
    assert owner.can_admin
    assert owner.sources == ("owner",)

    # Another member — even a tenant admin — gets nothing from a personal object.
    stranger = resolve_access(actor(STRANGER), personal)
    assert not stranger.can_read
    assert stranger.permission is None
    admin = resolve_access(actor(STRANGER, is_tenant_admin=True), personal)
    assert not admin.can_read


def test_department_scope_reads_for_current_members_only() -> None:
    department = ScopeGrant(scope="department", department_id=DEPARTMENT)
    inside = resolve_access(actor(MEMBER, department_id=DEPARTMENT), department)
    assert inside.can_read and not inside.can_write

    outside = resolve_access(actor(MEMBER, department_id=OTHER_DEPARTMENT), department)
    assert not outside.can_read
    detached = resolve_access(actor(MEMBER, department_id=None), department)
    assert not detached.can_read


def test_tenant_admin_is_admin_on_department_and_enterprise_only() -> None:
    admin = actor(STRANGER, department_id=None, is_tenant_admin=True)
    assert resolve_access(admin, ScopeGrant(scope="enterprise")).can_admin
    assert resolve_access(admin, ScopeGrant(scope="department", department_id=DEPARTMENT)).can_admin
    assert not resolve_access(admin, ScopeGrant(scope="personal", owner_user_id=OWNER)).can_read


def test_a_grant_is_additive_and_never_takes_permission_away() -> None:
    personal = ScopeGrant(scope="personal", owner_user_id=OWNER)

    read_grant = resolve_access(
        actor(MEMBER), personal, (AclGrant(permission="read", user_id=MEMBER),)
    )
    assert read_grant.can_read and not read_grant.can_write
    assert read_grant.sources == ("acl:read",)

    write_grant = resolve_access(
        actor(MEMBER), personal, (AclGrant(permission="write", user_id=MEMBER),)
    )
    assert write_grant.can_write and not write_grant.can_admin

    # The owner keeps admin when a read grant exists for somebody else.
    owner_with_other_grant = resolve_access(
        actor(OWNER), personal, (AclGrant(permission="read", user_id=MEMBER),)
    )
    assert owner_with_other_grant.can_admin
    assert owner_with_other_grant.rank == DEFAULT_PERMISSION_RANK[Permission.ADMIN]

    # Two weaker grants never sum into a stronger one.
    summed = resolve_access(
        actor(MEMBER),
        ScopeGrant(scope="enterprise"),
        (AclGrant(permission="read", user_id=MEMBER), AclGrant(permission="write", user_id=MEMBER)),
    )
    assert summed.permission == "write"


def test_a_department_grant_reaches_its_current_members() -> None:
    target = actor(MEMBER, department_id=DEPARTMENT)
    decision = resolve_access(
        target, None, (AclGrant(permission="write", department_id=DEPARTMENT),)
    )
    assert decision.can_write

    other = actor(MEMBER, department_id=OTHER_DEPARTMENT)
    assert not resolve_access(
        other, None, (AclGrant(permission="write", department_id=DEPARTMENT),)
    ).can_read

    # A detached member matches no department grant, not even a null one.
    detached = resolve_access(
        actor(MEMBER, department_id=None),
        None,
        (AclGrant(permission="read", department_id=DEPARTMENT),),
    )
    assert not detached.can_read


def test_an_unregistered_object_is_invisible_to_everyone() -> None:
    for user, is_admin in ((OWNER, False), (MEMBER, False), (STRANGER, True)):
        decision = resolve_access(actor(user, is_tenant_admin=is_admin), None)
        assert decision.permission is None
        assert decision.rank == 0
        assert not decision.can_read


def test_satisfies_uses_the_published_permission_order() -> None:
    read = resolve_access(actor(MEMBER), ScopeGrant(scope="enterprise"))
    assert read.satisfies("read")
    assert not read.satisfies("write")
    with pytest.raises(RbacModelError):
        read.satisfies("superuser")


@pytest.mark.parametrize(
    ("scope", "owner_user_id", "department_id"),
    [
        ("personal", None, None),
        ("personal", OWNER, DEPARTMENT),
        ("department", OWNER, DEPARTMENT),
        ("department", None, None),
        ("enterprise", OWNER, None),
        ("enterprise", None, DEPARTMENT),
        ("team", OWNER, None),
    ],
)
def test_scope_shape_refuses_impossible_combinations(
    scope: str, owner_user_id: int | None, department_id: str | None
) -> None:
    with pytest.raises(RbacModelError):
        validate_scope_shape(scope, owner_user_id=owner_user_id, department_id=department_id)


@pytest.mark.parametrize(
    ("scope", "owner_user_id", "department_id"),
    [
        ("personal", OWNER, None),
        ("department", None, DEPARTMENT),
        ("enterprise", None, None),
    ],
)
def test_scope_shape_accepts_the_three_layers(
    scope: str, owner_user_id: int | None, department_id: str | None
) -> None:
    assert (
        validate_scope_shape(scope, owner_user_id=owner_user_id, department_id=department_id)
        == scope
    )


def test_a_grant_names_exactly_one_subject() -> None:
    with pytest.raises(RbacModelError):
        AclGrant(permission="read")
    with pytest.raises(RbacModelError):
        AclGrant(permission="read", user_id=MEMBER, department_id=DEPARTMENT)
    with pytest.raises(RbacModelError):
        AclGrant(permission="owner", user_id=MEMBER)

    grant = AclGrant(permission="admin", department_id=DEPARTMENT)
    assert grant.subject_kind is AclSubjectKind.DEPARTMENT
    assert grant.subject_id == DEPARTMENT
    assert AclGrant(permission="read", user_id=MEMBER).subject_kind is AclSubjectKind.USER


def test_object_kind_is_shape_checked_but_not_frozen() -> None:
    assert validate_object_kind("workflow") == "workflow"
    assert validate_object_kind("  knowledge_base  ") == "knowledge_base"
    assert validate_object_kind("feature_flag_v2") == "feature_flag_v2"
    for bad in ("", "Workflow", "1workflow", "work-flow", "x" * 65, None):
        with pytest.raises(RbacModelError):
            validate_object_kind(bad)


def test_permission_parsing_normalises_case() -> None:
    assert parse_permission(" READ ") == "read"
    assert parse_permission(Permission.ADMIN) == "admin"
    with pytest.raises(RbacModelError):
        parse_permission("delete")


def test_visibility_fragment_binds_the_actor_subject_twice() -> None:
    """The list filter must match the resolver: owner, department, and grants."""
    member = actor(MEMBER, department_id=DEPARTMENT)
    sql, params = visibility_sql(member, scope_table="s", acl_table="a")

    assert "s.scope = 'enterprise'" in sql
    assert "s.owner_user_id = ?" in sql
    assert "s.department_id = ?" in sql
    assert "a.department_id = ?" in sql
    assert "workbuddy_object_acl a" in sql
    # user, department (scope arm), then the EXISTS clause: user, department.
    assert params == [MEMBER, DEPARTMENT, MEMBER, DEPARTMENT]

    # A detached member matches no department arm at all, so no department
    # parameter is bound and PostgreSQL has nothing ambiguous to infer.
    detached_sql, detached_params = visibility_sql(actor(MEMBER, department_id=None))
    assert "s.department_id = ?" not in detached_sql
    assert detached_sql.count("?") == len(detached_params) == 2
    assert detached_params == [MEMBER, MEMBER]


def test_visibility_fragment_keeps_a_tenant_admin_on_department_objects() -> None:
    """The filter must carry the admin rank the resolver grants, or the two drift."""
    plain_sql, _ = visibility_sql(actor(MEMBER, department_id=None))
    assert "s.scope = 'department'" not in plain_sql

    admin_sql, params = visibility_sql(actor(MEMBER, department_id=None, is_tenant_admin=True))
    assert "s.scope = 'department'" in admin_sql
    # The admin arm adds no parameter and personal objects stay private.
    assert params == [MEMBER, MEMBER]
    assert "s.owner_user_id = ?" in admin_sql

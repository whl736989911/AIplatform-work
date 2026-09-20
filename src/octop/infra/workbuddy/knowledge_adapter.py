"""Keep the personal-edition knowledge API working beside the tenant stack (B-12).

The personal edition and the WorkBuddy tenant stack store knowledge bases in two
different shapes. The personal side keeps one row per owner with ``shared`` and
``default_open`` flags; the tenant side keeps scoped rows with an embedding
revision resolved per tenant. Replacing one with the other outright is not an
option: a standalone install (SQLite, no tenant) has no tenant side at all, and
the personal pages are expected to keep working unchanged while both exist.

This module is that seam and nothing else:

* :func:`knowledge_source` reads ``OCTOP_KNOWLEDGE_SOURCE`` — ``personal`` (the
  default: the personal tables stay authoritative and nothing is mirrored),
  ``dual`` (write both, read the personal side) or ``enterprise`` (read the
  tenant side and project it back into the personal shape);
* :func:`mirror_base` upserts the tenant row for a personal base and
  :func:`project_bases` renders tenant rows as the personal payload;
* every mirroring step is best effort: a user without a tenant, a SQLite control
  plane, or a tenant without a granted embedding revision leaves the personal
  request untouched instead of failing it. Divergence is logged, and the switch
  keeps the rollback one environment variable away.

Documents are moved by the migration path (B-08/B-13), not here: this seam works
on the base rows the personal pages list.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_catalog import (
    CAPABILITY_MODEL,
    WorkBuddyCatalogRepo,
)
from octop.infra.db.repos.workbuddy_identity import WorkBuddyIdentityRepo
from octop.infra.db.repos.workbuddy_knowledge import (
    WorkBuddyKnowledgeBaseRow,
    WorkBuddyKnowledgeRepo,
)
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    WorkBuddyPostgresRequiredError,
)

logger = logging.getLogger(__name__)

SOURCE_PERSONAL = "personal"
SOURCE_DUAL = "dual"
SOURCE_ENTERPRISE = "enterprise"
SOURCES: tuple[str, ...] = (SOURCE_PERSONAL, SOURCE_DUAL, SOURCE_ENTERPRISE)

KNOWLEDGE_SOURCE_ENV = "OCTOP_KNOWLEDGE_SOURCE"

# The personal schema's own default, used when the tenant row has no cap of its
# own (``max_documents`` is a personal-edition column; B-10 carries it over).
DEFAULT_MAX_DOCUMENTS = 100

__all__ = [
    "DEFAULT_MAX_DOCUMENTS",
    "KNOWLEDGE_SOURCE_ENV",
    "SOURCE_DUAL",
    "SOURCE_ENTERPRISE",
    "SOURCE_PERSONAL",
    "SOURCES",
    "knowledge_source",
    "mirror_base",
    "mirror_enabled",
    "mirror_target",
    "project_bases",
    "project_base",
    "project_enabled",
]


def knowledge_source(raw: str | None = None) -> str:
    """The configured knowledge source; an unknown value falls back to ``personal``.

    Falling back to the personal tables is the safe direction: it is the shape
    every existing install already serves, so a typo can never silently switch a
    deployment onto tables it has not populated.
    """
    value = (raw if raw is not None else os.environ.get(KNOWLEDGE_SOURCE_ENV, "")).strip().lower()
    if not value:
        return SOURCE_PERSONAL
    if value in SOURCES:
        return value
    logger.warning(
        "unknown %s=%r; falling back to %s", KNOWLEDGE_SOURCE_ENV, value, SOURCE_PERSONAL
    )
    return SOURCE_PERSONAL


def mirror_enabled(source: str) -> bool:
    """True when a personal write must be mirrored into the tenant tables."""
    return source in (SOURCE_DUAL, SOURCE_ENTERPRISE)


def project_enabled(source: str) -> bool:
    """True when the personal read path must project the tenant tables instead."""
    return source == SOURCE_ENTERPRISE


def mirror_target(server: Any, user_id: int) -> WorkBuddyDbContext | None:
    """The caller's tenant context, or ``None`` when mirroring cannot apply.

    ``None`` covers the three cases that are normal rather than exceptional: a
    SQLite control plane, a user who is not a member of any tenant, and a
    control plane whose service bundle is not available yet.
    """
    db = _database(server)
    if db is None:
        return None
    try:
        member = WorkBuddyIdentityRepo(db).membership_for_user(user_id)
    except WorkBuddyPostgresRequiredError:
        return None
    except Exception as exc:  # noqa: BLE001 - mirroring must never break the caller
        logger.warning("knowledge mirror: cannot resolve a tenant for user %s: %s", user_id, exc)
        return None
    if not member:
        return None
    tenant_id = str(member.get("tenant_id") or "")
    if not tenant_id:
        return None
    return WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id)


def mirror_base(server: Any, *, user_id: int, base: Any) -> str | None:
    """Upsert the tenant row for one personal base; ``None`` when it cannot apply.

    Matching is by owner and name, because the personal schema has no tenant-side
    identifier: ``id`` on the personal row is the personal key, not the tenant's
    ``kb_id``. B-13 replaces this heuristic with an explicit mapping when the
    migration writes its own linkage.
    """
    ctx = mirror_target(server, user_id)
    if ctx is None:
        return None
    db = _database(server)
    if db is None:
        return None
    try:
        repo = WorkBuddyKnowledgeRepo(db)
        existing = _matched_base(repo, ctx, user_id=user_id, name=str(base.name))
        if existing is not None:
            return existing.kb_id
        model = _usable_model_revision(db, ctx)
        if model is None:
            logger.warning(
                "knowledge mirror: tenant %s has no granted embedding revision; base %r stays personal",
                ctx.tenant_id,
                base.name,
            )
            return None
        # A shared base becomes a company base, and the tenant schema's shape check
        # only allows an owner on a personal scope: a shared row carries no owner.
        shared = bool(getattr(base, "shared", False))
        row = repo.create_base(
            ctx,
            scope=SOURCE_ENTERPRISE if shared else SOURCE_PERSONAL,
            name=str(base.name),
            description=str(getattr(base, "description", "") or ""),
            model=model,
            created_by_user_id=user_id,
            owner_user_id=None if shared else user_id,
        )
        return row.kb_id
    except WorkBuddyPostgresRequiredError:
        return None
    except Exception as exc:  # noqa: BLE001 - the personal write must still succeed
        logger.warning("knowledge mirror: base %r was not mirrored: %s", base.name, exc)
        return None


def project_bases(server: Any, *, user_id: int, is_admin: bool) -> list[dict[str, Any]] | None:
    """The caller's tenant bases as personal payloads, or ``None`` when unavailable.

    ``None`` means the caller has no tenant side to read (or the control plane is
    SQLite), and the caller should serve the personal tables as before.
    """
    ctx = mirror_target(server, user_id)
    if ctx is None:
        return None
    db = _database(server)
    if db is None:
        return None
    try:
        repo = WorkBuddyKnowledgeRepo(db)
        rows = repo.list_bases(ctx)
        documents = {row.kb_id: repo.list_documents(ctx, row.kb_id) for row in rows}
    except WorkBuddyPostgresRequiredError:
        return None
    except Exception as exc:  # noqa: BLE001 - a read must degrade, not fail
        logger.warning("knowledge projection: cannot read tenant bases: %s", exc)
        return None
    visible = [
        row
        for row in rows
        if is_admin or row.scope != SOURCE_PERSONAL or row.owner_user_id == user_id
    ]
    return [project_base(row, doc_count=len(documents.get(row.kb_id, ()))) for row in visible]


def project_base(row: WorkBuddyKnowledgeBaseRow, *, doc_count: int = 0) -> dict[str, Any]:
    """Render one tenant row in the personal payload shape.

    Fields the tenant schema does not carry yet are projected with the personal
    edition's own defaults: ``default_open`` is a preference (B-13 maps it),
    ``icon_name`` has no tenant column, and ``max_documents`` falls back to the
    personal default until B-10 carries the cap over.
    """
    return {
        "id": row.kb_id,
        "knowledge_base_id": row.kb_id,
        # A company base has no owner on the tenant side; its creator owns it in
        # the personal view, which is what the personal payload expects.
        "owner_user_id": row.owner_user_id
        if row.owner_user_id is not None
        else row.created_by_user_id,
        "name": row.name,
        "description": row.description,
        "default_open": False,
        "shared": row.scope != SOURCE_PERSONAL,
        "icon_name": "",
        "embedding_model": row.embedding_model_key,
        "embedding_dim": row.embedding_dimensions,
        "doc_count": int(doc_count),
        "max_documents": DEFAULT_MAX_DOCUMENTS,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _database(server: Any) -> DatabasePool | None:
    services = getattr(server, "services", None)
    db = getattr(services, "db", None)
    return db if isinstance(db, DatabasePool) else None


def _matched_base(
    repo: WorkBuddyKnowledgeRepo, ctx: WorkBuddyDbContext, *, user_id: int, name: str
) -> WorkBuddyKnowledgeBaseRow | None:
    """The tenant twin of a personal base, matched by creator and name.

    ``created_by_user_id`` rather than ``owner_user_id``: a shared base mirrors as a
    company base, which by the tenant schema's shape check carries no owner at all.
    """
    for row in repo.list_bases(ctx):
        if row.created_by_user_id == user_id and row.name == name:
            return row
    return None


def _usable_model_revision(db: DatabasePool, ctx: WorkBuddyDbContext) -> Any | None:
    """A published embedding revision the tenant granted, newest key first.

    The personal base's own ``embedding_model`` is preferred when it maps onto a
    granted revision, so a mirrored base keeps the vectors it already has.
    """
    catalog = WorkBuddyCatalogRepo(db)
    for revision in catalog.list_public_models():
        if catalog.granted_tenant_wide(
            str(ctx.tenant_id), kind=CAPABILITY_MODEL, revision_id=revision.model_revision_id
        ):
            return revision
    return None

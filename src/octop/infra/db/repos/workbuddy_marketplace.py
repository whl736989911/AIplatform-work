"""WorkBuddy marketplace persistence (PostgreSQL only).

Schema: ``021_workbuddy_marketplace.pg.sql``. This repository owns the marketplace
rows — published templates and their immutable versions, developer submissions
and their review trail, tenant installations, credential bindings, consent
evidence, explicit upgrades and the marketplace job rows in the shared
``workbuddy_jobs`` table.

Guarantees enforced here on top of the schema:

* every tenant read/write runs through :func:`workbuddy_transaction` with the
  tenant applied to ``app.tenant_id``; each statement repeats ``tenant_id =
  ?`` so isolation does not rely on RLS alone;
* a scoped miss and a cross-tenant reference are indistinguishable: both return
  ``None``, and callers surface one uniform not-found;
* frozen submission content, published template versions, review decisions and
  consent evidence are append-only (the triggers in 021 back this up);
* installation and upgrade updates compare-and-swap on ``revision`` so two
  concurrent installs cannot both win.

Failures raise :class:`WorkBuddyMarketplaceError` carrying a stable code; the
service maps that to an :class:`~octop.infra.errors.ErrorCode` member.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Literal

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    coerce_workbuddy_context,
    normalize_uuid,
    require_postgres,
    workbuddy_transaction,
)

ERROR_MARKETPLACE_NOT_FOUND = "WORKBUDDY_MARKETPLACE_NOT_FOUND"
ERROR_SUBMISSION_INVALID = "WORKBUDDY_SUBMISSION_INVALID"
ERROR_SUBMISSION_FROZEN = "WORKBUDDY_SUBMISSION_FROZEN"
ERROR_SUBMISSION_NOT_APPROVABLE = "WORKBUDDY_SUBMISSION_NOT_APPROVABLE"
ERROR_VERSION_IMMUTABLE = "WORKBUDDY_VERSION_IMMUTABLE"
ERROR_INVALID_ARGUMENT = "WORKBUDDY_INVALID_ARGUMENT"
ERROR_CONFLICT = "STATE_CONFLICT"
ERROR_DEPENDENCY_UNAVAILABLE = "WORKBUDDY_DEPENDENCY_UNAVAILABLE"

SUBMISSION_STATUSES = ("draft", "submitted", "approved", "rejected")
INSTALLATION_STATUSES = ("pending", "installing", "installed", "failed")
UPGRADE_STATUSES = ("pending", "installing", "installed", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
TEMPLATE_STATUSES = ("published", "withdrawn")
REVIEW_DECISIONS = ("approved", "rejected")
CONSENT_SUBJECTS = ("install", "upgrade")

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 100

_SQLSTATE_UNIQUE = "23505"
_SQLSTATE_CHECK = "23514"
_SQLSTATE_FK = "23503"
_SQLSTATE_APPEND_ONLY = "42501"

_SUBMISSION_COLUMNS = (
    "id, tenant_id, submitted_by, submitted_by_user_id, name, summary, industry, "
    "definition, definition_hash, license_id, license_text_hash, requested_capabilities, "
    "status, frozen_definition, frozen_definition_hash, review_note, platform_review_ref, "
    "published_template_version_id, submitted_at, reviewed_at, reviewed_by_user_id, "
    "revision, created_at, updated_at"
)
_INSTALLATION_COLUMNS = (
    "id, tenant_id, template_id, template_version_id, workflow_id, installed_version_id, "
    "installed_by, installed_by_user_id, status, consented_license_hash, consented_capabilities, "
    "consented_at, job_id, error_code, error_detail, revision, created_at, updated_at"
)
_UPGRADE_COLUMNS = (
    "id, tenant_id, installation_id, from_template_version_id, to_template_version_id, "
    "workflow_id, workflow_version_id, requested_by, requested_by_user_id, status, "
    "consented_license_hash, consented_capabilities, consented_at, job_id, error_code, "
    "error_detail, created_at, updated_at"
)
_JOB_COLUMNS = (
    "id, tenant_id, kind, status, progress, requested_by_user_id, idempotency_key, "
    "request_hash, result, error_code, error_message, created_at, started_at, finished_at"
)


class WorkBuddyMarketplaceError(ValueError):
    """A marketplace persistence refusal carrying a stable code."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})


def _sqlstate(exc: Exception) -> str | None:
    return getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)


@contextmanager
def _db_errors(*, code: str = ERROR_INVALID_ARGUMENT) -> Iterator[None]:
    """Translate database refusals into stable marketplace codes."""
    try:
        yield
    except WorkBuddyMarketplaceError:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below, never leaked raw
        state = _sqlstate(exc)
        if state == _SQLSTATE_APPEND_ONLY:
            raise WorkBuddyMarketplaceError(
                ERROR_VERSION_IMMUTABLE,
                "published marketplace content and its evidence are append only",
            ) from exc
        if state == _SQLSTATE_UNIQUE:
            raise WorkBuddyMarketplaceError(
                ERROR_CONFLICT, "the marketplace row conflicts with an existing one"
            ) from exc
        if state == _SQLSTATE_FK:
            raise WorkBuddyMarketplaceError(
                code, "the request references an object this tenant does not have"
            ) from exc
        if state == _SQLSTATE_CHECK:
            raise WorkBuddyMarketplaceError(
                code, "the marketplace row violates its contract"
            ) from exc
        raise


def _jsonb(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (Mapping, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return default
    return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _uuid_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _limit(value: Any, default: int = DEFAULT_LIST_LIMIT) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = default
    return max(1, min(size, MAX_LIST_LIMIT))


def _offset(value: Any) -> int:
    try:
        start = int(value)
    except (TypeError, ValueError):
        start = 0
    return max(0, start)


def new_id() -> str:
    """New public UUID for a marketplace row (jobs carry no database default)."""
    return str(uuid.uuid4())


# ── row projections ──────────────────────────────────────────────────────────


def submission_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "tenant_id": _uuid_text(row["tenant_id"]),
        "submitted_by": _uuid_text(row["submitted_by"]),
        "submitted_by_user_id": row["submitted_by_user_id"],
        "name": row["name"],
        "summary": row["summary"],
        "industry": row["industry"],
        "definition": _load_json(row["definition"], {}),
        "definition_hash": row["definition_hash"],
        "license_id": row["license_id"],
        "license_text_hash": row["license_text_hash"],
        "requested_capabilities": _load_json(row["requested_capabilities"], []),
        "status": row["status"],
        "frozen_definition": _load_json(row["frozen_definition"], None),
        "frozen_definition_hash": row["frozen_definition_hash"],
        "review_note": row["review_note"],
        "platform_review_ref": row["platform_review_ref"],
        "published_template_version_id": _uuid_text(row["published_template_version_id"]),
        "submitted_at": _iso(row["submitted_at"]),
        "reviewed_at": _iso(row["reviewed_at"]),
        "reviewed_by_user_id": row["reviewed_by_user_id"],
        "revision": int(row["revision"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def _template_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "slug": row["slug"],
        "name": row["name"],
        "description": row["description"],
        "industry": row["industry"],
        "publisher_display": row["publisher_display"],
        "status": row["status"],
        "current_version_id": _uuid_text(row["current_version_id"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def version_dict(row: DbRow) -> dict[str, Any]:
    return {
        "template_id": _uuid_text(row["template_id"]),
        "template_version_id": _uuid_text(row["id"]),
        "version": row["version"],
        "definition": _load_json(row["definition"], {}),
        "definition_hash": row["definition_hash"],
        "schema_version": int(row["schema_version"]),
        "license_id": row["license_id"],
        "license_text_hash": row["license_text_hash"],
        "required_capabilities": _load_json(row["required_capabilities"], []),
        "content_summary": row["content_summary"],
        "published_at": _iso(row["published_at"]),
    }


def installation_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "tenant_id": _uuid_text(row["tenant_id"]),
        "template_id": _uuid_text(row["template_id"]),
        "template_version_id": _uuid_text(row["template_version_id"]),
        "workflow_id": _uuid_text(row["workflow_id"]),
        "installed_version_id": _uuid_text(row["installed_version_id"]),
        "installed_by": _uuid_text(row["installed_by"]),
        "installed_by_user_id": row["installed_by_user_id"],
        "status": row["status"],
        "consented_license_hash": row["consented_license_hash"],
        "consented_capabilities": _load_json(row["consented_capabilities"], None),
        "consented_at": _iso(row["consented_at"]),
        "job_id": _uuid_text(row["job_id"]),
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
        "revision": int(row["revision"]),
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def upgrade_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "tenant_id": _uuid_text(row["tenant_id"]),
        "installation_id": _uuid_text(row["installation_id"]),
        "from_template_version_id": _uuid_text(row["from_template_version_id"]),
        "to_template_version_id": _uuid_text(row["to_template_version_id"]),
        "workflow_id": _uuid_text(row["workflow_id"]),
        "workflow_version_id": _uuid_text(row["workflow_version_id"]),
        "requested_by": _uuid_text(row["requested_by"]),
        "requested_by_user_id": row["requested_by_user_id"],
        "status": row["status"],
        "consented_license_hash": row["consented_license_hash"],
        "consented_capabilities": _load_json(row["consented_capabilities"], None),
        "consented_at": _iso(row["consented_at"]),
        "job_id": _uuid_text(row["job_id"]),
        "error_code": row["error_code"],
        "error_detail": row["error_detail"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def consent_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "installation_id": _uuid_text(row["installation_id"]),
        "subject_kind": row["subject_kind"],
        "subject_id": _uuid_text(row["subject_id"]),
        "template_id": _uuid_text(row["template_id"]),
        "template_version_id": _uuid_text(row["template_version_id"]),
        "license_id": row["license_id"],
        "license_text_hash": row["license_text_hash"],
        "capabilities": _load_json(row["capabilities"], []),
        "capabilities_hash": row["capabilities_hash"],
        "consented_by": _uuid_text(row["consented_by"]),
        "consented_by_user_id": row["consented_by_user_id"],
        "consented_at": _iso(row["consented_at"]),
    }


def review_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "submission_id": _uuid_text(row["submission_id"]),
        "decision": row["decision"],
        "note": row["note"],
        "platform_review_ref": row["platform_review_ref"],
        "reviewer_user_id": row["reviewer_user_id"],
        "published_template_version_id": _uuid_text(row["published_template_version_id"]),
        "created_at": _iso(row["created_at"]),
    }


def binding_dict(row: DbRow) -> dict[str, Any]:
    return {
        "binding_key": row["binding_key"],
        "credential_id": _uuid_text(row["credential_id"]),
        "created_at": _iso(row["created_at"]),
    }


def job_dict(row: DbRow) -> dict[str, Any]:
    return {
        "id": _uuid_text(row["id"]),
        "kind": row["kind"],
        "status": row["status"],
        "progress": int(row["progress"]),
        "requested_by_user_id": row["requested_by_user_id"],
        "result": _load_json(row["result"], None),
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "created_at": _iso(row["created_at"]),
        "started_at": _iso(row["started_at"]),
        "finished_at": _iso(row["finished_at"]),
    }


class _AmbientTransaction:
    """Adapter for joining a caller-owned transaction (``conn=`` keyword)."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, *exc_info: object) -> Literal[False]:
        return False


class WorkBuddyMarketplaceRepo:
    """Marketplace persistence.

    Construction is cheap and fails closed on non-PostgreSQL pools. Every method
    owns its transaction unless the caller passes ``conn=`` to join an ambient
    :func:`~octop.infra.db.workbuddy_context.workbuddy_transaction` (which is how
    an install keeps its installation row and the created workflow version atomic).
    """

    def __init__(self, db: DatabasePool) -> None:
        require_postgres(db)
        self._db = db

    # ── context helpers ──────────────────────────────────────────────────────

    def _tenant_context(
        self, tenant_id: object, *, user_id: object = None, ctx: Any = None
    ) -> WorkBuddyDbContext:
        if ctx is not None:
            return coerce_workbuddy_context(ctx)
        return WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id)

    @staticmethod
    def _platform_context() -> WorkBuddyDbContext:
        return WorkBuddyDbContext.platform()

    def _transaction(self, ctx: WorkBuddyDbContext, conn: Any = None) -> Any:
        if conn is not None:
            return _AmbientTransaction(conn)
        return workbuddy_transaction(self._db, ctx)

    # ── published catalog (global) ───────────────────────────────────────────

    def list_published_templates(
        self, *, industry: object = None, limit: object = None, offset: object = None
    ) -> list[dict[str, Any]]:
        clauses = [" WHERE t.status = 'published'"]
        params: list[object] = []
        if industry is not None and str(industry).strip():
            clauses.append(" AND t.industry = ?")
            params.append(str(industry).strip())
        params.extend([_limit(limit), _offset(offset)])
        sql = (
            "SELECT t.id, t.slug, t.name, t.description, t.industry, t.publisher_display, "
            "t.status, t.current_version_id, t.created_at, t.updated_at, "
            "v.version AS current_version, v.license_id, v.content_summary, "
            "v.required_capabilities, v.published_at "
            "FROM marketplace_templates t "
            "LEFT JOIN marketplace_template_versions v ON v.template_id = t.id AND v.id = t.current_version_id"
            f"{''.join(clauses)} ORDER BY t.industry, t.name LIMIT ? OFFSET ?"
        )
        with _db_errors(), workbuddy_transaction(self._db, self._platform_context()) as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = _template_dict(row)
            item["current_version"] = row["current_version"]
            item["license_id"] = row["license_id"]
            item["content_summary"] = row["content_summary"]
            item["required_capabilities"] = _load_json(row["required_capabilities"], [])
            item["published_at"] = _iso(row["published_at"])
            out.append(item)
        return out

    def get_template(self, template_id: object) -> dict[str, Any] | None:
        identifier = normalize_uuid(template_id, field="template_id")
        with _db_errors(), workbuddy_transaction(self._db, self._platform_context()) as conn:
            row = conn.execute(
                "SELECT id, slug, name, description, industry, publisher_display, status, "
                "current_version_id, created_at, updated_at FROM marketplace_templates WHERE id = ?",
                (identifier,),
            ).fetchone()
        return _template_dict(row) if row else None

    def get_published_version(
        self, template_id: object, version_id: object
    ) -> dict[str, Any] | None:
        """One published version of a published template, or ``None``.

        A withdrawn template, an unpublished version and a cross-tenant guess all
        return ``None`` so the caller can answer one uniform not-found.
        """
        identifier = normalize_uuid(template_id, field="template_id")
        version = normalize_uuid(version_id, field="version_id")
        with _db_errors(), workbuddy_transaction(self._db, self._platform_context()) as conn:
            row = conn.execute(
                "SELECT v.id, v.template_id, v.version, v.definition, v.definition_hash, "
                "v.schema_version, v.license_id, v.license_text_hash, v.required_capabilities, "
                "v.content_summary, v.published_at, t.status AS template_status "
                "FROM marketplace_template_versions v "
                "JOIN marketplace_templates t ON t.id = v.template_id "
                "WHERE v.template_id = ? AND v.id = ?",
                (identifier, version),
            ).fetchone()
        if row is None or str(row["template_status"]) != "published":
            return None
        return version_dict(row)

    def list_template_versions(self, template_id: object) -> list[dict[str, Any]]:
        identifier = normalize_uuid(template_id, field="template_id")
        with _db_errors(), workbuddy_transaction(self._db, self._platform_context()) as conn:
            rows = conn.execute(
                "SELECT id, template_id, version, definition, definition_hash, schema_version, "
                "license_id, license_text_hash, required_capabilities, content_summary, published_at "
                "FROM marketplace_template_versions WHERE template_id = ? "
                "ORDER BY published_at DESC, version DESC",
                (identifier,),
            ).fetchall()
        return [version_dict(row) for row in rows]

    def publish_template_version(
        self,
        *,
        slug: object,
        name: object,
        description: object,
        industry: object,
        publisher_display: object,
        version: object,
        definition: Mapping[str, Any],
        definition_hash: str,
        license_id: object,
        license_text_hash: str,
        required_capabilities: Sequence[Mapping[str, Any]],
        content_summary: object,
        reviewer_note: object = None,
        published_by_user_id: int | None,
        origin_tenant_id: object = None,
        origin_submission_id: object = None,
        existing_template_id: object = None,
    ) -> dict[str, Any]:
        """Publish one immutable version (platform review path).

        A first publication creates the template row in the same transaction; a
        later publication appends a version and moves ``current_version_id``.
        Existing versions are never rewritten.
        """
        clean_slug = str(slug or "").strip()
        clean_version = str(version or "").strip()
        template_id = (
            normalize_uuid(existing_template_id, field="template_id")
            if existing_template_id
            else None
        )
        with (
            _db_errors(code=ERROR_SUBMISSION_INVALID),
            workbuddy_transaction(self._db, self._platform_context()) as conn,
        ):
            if template_id is None:
                row = conn.execute(
                    "INSERT INTO marketplace_templates("
                    "slug, name, description, industry, publisher_display, status, "
                    "origin_tenant_id, origin_submission_id, current_version_id"
                    ") VALUES (?, ?, ?, ?, ?, 'withdrawn', ?, ?, NULL) RETURNING id",
                    (
                        clean_slug,
                        str(name),
                        str(description or ""),
                        str(industry or ""),
                        str(publisher_display),
                        None if origin_tenant_id is None else str(origin_tenant_id),
                        None if origin_submission_id is None else str(origin_submission_id),
                    ),
                ).fetchone()
                if row is None:
                    raise WorkBuddyMarketplaceError(
                        ERROR_DEPENDENCY_UNAVAILABLE, "template row was not created"
                    )
                template_id = str(row["id"])
            version_row = conn.execute(
                "INSERT INTO marketplace_template_versions("
                "template_id, version, definition, definition_hash, schema_version, license_id, "
                "license_text_hash, required_capabilities, content_summary, reviewer_note, "
                "published_by_user_id, origin_tenant_id, origin_submission_id"
                ") VALUES (?, ?, ?::jsonb, ?, 1, ?, ?, ?::jsonb, ?, ?, ?, ?, ?) "
                "RETURNING id, template_id, version, definition, definition_hash, schema_version, "
                "license_id, license_text_hash, required_capabilities, content_summary, published_at",
                (
                    template_id,
                    clean_version,
                    _jsonb(definition),
                    str(definition_hash),
                    str(license_id),
                    str(license_text_hash),
                    _jsonb(list(required_capabilities)),
                    str(content_summary or ""),
                    None if reviewer_note is None else str(reviewer_note),
                    published_by_user_id,
                    None if origin_tenant_id is None else str(origin_tenant_id),
                    None if origin_submission_id is None else str(origin_submission_id),
                ),
            ).fetchone()
            if version_row is None:
                raise WorkBuddyMarketplaceError(
                    ERROR_DEPENDENCY_UNAVAILABLE, "template version row was not created"
                )
            version_id = str(version_row["id"])
            updated = conn.execute(
                "UPDATE marketplace_templates SET current_version_id = ?, status = 'published', "
                "updated_at = now() WHERE id = ? RETURNING id",
                (version_id, template_id),
            ).fetchone()
            if updated is None:
                raise WorkBuddyMarketplaceError(ERROR_MARKETPLACE_NOT_FOUND, "template not found")
        version_payload = version_dict(version_row)
        version_payload["template_slug"] = clean_slug
        version_payload["template_name"] = str(name)
        return version_payload

    def withdraw_template(self, template_id: object) -> dict[str, Any] | None:
        identifier = normalize_uuid(template_id, field="template_id")
        with _db_errors(), workbuddy_transaction(self._db, self._platform_context()) as conn:
            row = conn.execute(
                "UPDATE marketplace_templates SET status = 'withdrawn', updated_at = now() "
                "WHERE id = ? RETURNING id, slug, name, description, industry, publisher_display, "
                "status, current_version_id, created_at, updated_at",
                (identifier,),
            ).fetchone()
        return _template_dict(row) if row else None

    # ── developer submissions (tenant) ───────────────────────────────────────

    def create_submission(
        self,
        tenant_id: object,
        *,
        submitted_by: object,
        submitted_by_user_id: int | None,
        name: object,
        summary: object,
        industry: object,
        definition: Mapping[str, Any],
        definition_hash: str,
        license_id: object,
        license_text_hash: str,
        requested_capabilities: Sequence[Mapping[str, Any]],
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        member_id = normalize_uuid(submitted_by, field="submitted_by")
        context = self._tenant_context(identifier, user_id=submitted_by_user_id, ctx=ctx)
        with _db_errors(code=ERROR_SUBMISSION_INVALID), self._transaction(context, conn) as active:
            row = active.execute(
                "INSERT INTO developer_submissions("
                "tenant_id, submitted_by, submitted_by_user_id, name, summary, industry, "
                "definition, definition_hash, license_id, license_text_hash, requested_capabilities, "
                "status, revision) VALUES (?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?, ?::jsonb, 'draft', 1) "
                f"RETURNING {_SUBMISSION_COLUMNS}",
                (
                    identifier,
                    member_id,
                    submitted_by_user_id,
                    str(name),
                    str(summary or ""),
                    str(industry or ""),
                    _jsonb(definition),
                    str(definition_hash),
                    str(license_id),
                    str(license_text_hash),
                    _jsonb(list(requested_capabilities)),
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyMarketplaceError(
                ERROR_DEPENDENCY_UNAVAILABLE, "submission row was not created"
            )
        return submission_dict(row)

    def get_submission(
        self, tenant_id: object, submission_id: object, *, ctx: Any = None, conn: Any = None
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        submission = normalize_uuid(submission_id, field="submission_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"SELECT {_SUBMISSION_COLUMNS} FROM developer_submissions "
                "WHERE tenant_id = ? AND id = ?",
                (identifier, submission),
            ).fetchone()
        return submission_dict(row) if row else None

    def list_submissions(
        self,
        tenant_id: object,
        *,
        status: object = None,
        author_member_id: object = None,
        limit: object = None,
        offset: object = None,
        ctx: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        clauses = [" WHERE tenant_id = ?"]
        params: list[object] = [identifier]
        if status is not None and str(status).strip():
            clauses.append(" AND status = ?")
            params.append(str(status).strip())
        if author_member_id is not None and str(author_member_id).strip():
            clauses.append(" AND submitted_by = ?")
            params.append(normalize_uuid(author_member_id, field="author_member_id"))
        params.extend([_limit(limit), _offset(offset)])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                f"SELECT {_SUBMISSION_COLUMNS} FROM developer_submissions{''.join(clauses)} "
                "ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [submission_dict(row) for row in rows]

    def freeze_submission(
        self,
        tenant_id: object,
        submission_id: object,
        *,
        expected_revision: int | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Freeze a draft: copy the sanitized content and move to ``submitted``."""
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        submission = normalize_uuid(submission_id, field="submission_id")
        context = self._tenant_context(identifier, ctx=ctx)
        clauses = [
            "SET status = 'submitted', frozen_definition = definition, "
            "frozen_definition_hash = definition_hash, submitted_at = now(), "
            "updated_at = now(), revision = revision + 1",
            " WHERE tenant_id = ? AND id = ? AND status = 'draft'",
        ]
        params: list[object] = [identifier, submission]
        if expected_revision is not None:
            clauses.append(" AND revision = ?")
            params.append(int(expected_revision))
        with _db_errors(code=ERROR_SUBMISSION_INVALID), self._transaction(context, conn) as active:
            row = active.execute(
                f"UPDATE developer_submissions {''.join(clauses)} RETURNING {_SUBMISSION_COLUMNS}",
                tuple(params),
            ).fetchone()
            if row is None:
                current = active.execute(
                    "SELECT status FROM developer_submissions WHERE tenant_id = ? AND id = ?",
                    (identifier, submission),
                ).fetchone()
                if current is None:
                    raise WorkBuddyMarketplaceError(
                        ERROR_MARKETPLACE_NOT_FOUND, "submission not found"
                    )
                if str(current["status"]) != "draft":
                    raise WorkBuddyMarketplaceError(
                        ERROR_SUBMISSION_FROZEN, "the submitted content is already frozen"
                    )
                raise WorkBuddyMarketplaceError(
                    ERROR_CONFLICT, "the submission changed since it was read"
                )
        return submission_dict(row)

    def decide_submission(
        self,
        tenant_id: object,
        submission_id: object,
        *,
        decision: str,
        note: object = None,
        platform_review_ref: str,
        reviewer_user_id: int | None,
        published_template_version_id: object = None,
        expected_revision: int | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Record an append-only review decision and move the submission state."""
        if decision not in REVIEW_DECISIONS:
            raise WorkBuddyMarketplaceError(
                ERROR_SUBMISSION_INVALID, "review decision is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        submission = normalize_uuid(submission_id, field="submission_id")
        version_pointer = (
            normalize_uuid(published_template_version_id, field="published_template_version_id")
            if published_template_version_id
            else None
        )
        context = self._tenant_context(identifier, user_id=reviewer_user_id, ctx=ctx)
        clauses = [
            "SET status = ?, review_note = ?, platform_review_ref = ?, "
            "published_template_version_id = ?, reviewed_at = now(), "
            "reviewed_by_user_id = ?, updated_at = now(), revision = revision + 1",
            " WHERE tenant_id = ? AND id = ? AND status = 'submitted'",
        ]
        params: list[object] = [
            decision,
            None if note is None else str(note),
            str(platform_review_ref),
            version_pointer,
            reviewer_user_id,
            identifier,
            submission,
        ]
        if expected_revision is not None:
            clauses.append(" AND revision = ?")
            params.append(int(expected_revision))
        with _db_errors(code=ERROR_SUBMISSION_INVALID), self._transaction(context, conn) as active:
            row = active.execute(
                f"UPDATE developer_submissions {''.join(clauses)} RETURNING {_SUBMISSION_COLUMNS}",
                tuple(params),
            ).fetchone()
            if row is None:
                current = active.execute(
                    "SELECT status FROM developer_submissions WHERE tenant_id = ? AND id = ?",
                    (identifier, submission),
                ).fetchone()
                if current is None:
                    raise WorkBuddyMarketplaceError(
                        ERROR_MARKETPLACE_NOT_FOUND, "submission not found"
                    )
                raise WorkBuddyMarketplaceError(
                    ERROR_SUBMISSION_NOT_APPROVABLE,
                    "only a submitted recommendation can be reviewed",
                )
            review = active.execute(
                "INSERT INTO developer_submission_reviews("
                "tenant_id, submission_id, decision, note, platform_review_ref, "
                "reviewer_user_id, published_template_version_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?) "
                "RETURNING id, submission_id, decision, note, platform_review_ref, "
                "reviewer_user_id, published_template_version_id, created_at",
                (
                    identifier,
                    submission,
                    decision,
                    None if note is None else str(note),
                    str(platform_review_ref),
                    reviewer_user_id,
                    version_pointer,
                ),
            ).fetchone()
        if review is None:
            raise WorkBuddyMarketplaceError(
                ERROR_DEPENDENCY_UNAVAILABLE, "review row was not created"
            )
        return submission_dict(row), review_dict(review)

    def list_submission_reviews(
        self, tenant_id: object, submission_id: object, *, ctx: Any = None
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        submission = normalize_uuid(submission_id, field="submission_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                "SELECT id, submission_id, decision, note, platform_review_ref, reviewer_user_id, "
                "published_template_version_id, created_at FROM developer_submission_reviews "
                "WHERE tenant_id = ? AND submission_id = ? ORDER BY created_at DESC, id",
                (identifier, submission),
            ).fetchall()
        return [review_dict(row) for row in rows]

    # ── installations (tenant) ───────────────────────────────────────────────

    def create_installation(
        self,
        tenant_id: object,
        *,
        template_id: object,
        template_version_id: object,
        installed_by: object,
        installed_by_user_id: int | None,
        status: str = "pending",
        job_id: object = None,
        consented_license_hash: str | None = None,
        consented_capabilities: Sequence[Mapping[str, Any]] | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status not in INSTALLATION_STATUSES:
            raise WorkBuddyMarketplaceError(
                ERROR_INVALID_ARGUMENT, "installation status is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        template = normalize_uuid(template_id, field="template_id")
        version = normalize_uuid(template_version_id, field="template_version_id")
        member_id = normalize_uuid(installed_by, field="installed_by")
        context = self._tenant_context(identifier, user_id=installed_by_user_id, ctx=ctx)
        capabilities_json = (
            None if consented_capabilities is None else _jsonb(list(consented_capabilities))
        )
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                "INSERT INTO marketplace_installations("
                "tenant_id, template_id, template_version_id, installed_by, installed_by_user_id, "
                "status, job_id, consented_license_hash, consented_capabilities, consented_at, revision"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "CASE WHEN ?::jsonb IS NULL THEN NULL ELSE ?::jsonb END, "
                "CASE WHEN ?::jsonb IS NULL THEN NULL ELSE now() END, 1) "
                f"RETURNING {_INSTALLATION_COLUMNS}",
                (
                    identifier,
                    template,
                    version,
                    member_id,
                    installed_by_user_id,
                    status,
                    None if job_id is None else str(job_id),
                    consented_license_hash,
                    capabilities_json,
                    capabilities_json,
                    capabilities_json,
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyMarketplaceError(
                ERROR_DEPENDENCY_UNAVAILABLE, "installation row was not created"
            )
        return installation_dict(row)

    def get_installation(
        self, tenant_id: object, installation_id: object, *, ctx: Any = None, conn: Any = None
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM marketplace_installations "
                "WHERE tenant_id = ? AND id = ?",
                (identifier, installation),
            ).fetchone()
        return installation_dict(row) if row else None

    def list_installations(
        self,
        tenant_id: object,
        *,
        status: object = None,
        installed_by: object = None,
        limit: object = None,
        offset: object = None,
        ctx: Any = None,
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        clauses = [" WHERE tenant_id = ?"]
        params: list[object] = [identifier]
        if status is not None and str(status).strip():
            clauses.append(" AND status = ?")
            params.append(str(status).strip())
        if installed_by is not None and str(installed_by).strip():
            # An installation is readable by whoever installed it and by tenant
            # admins; a member's ledger is therefore the rows they own.
            clauses.append(" AND installed_by = ?")
            params.append(normalize_uuid(installed_by, field="installed_by"))
        params.extend([_limit(limit), _offset(offset)])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                f"SELECT {_INSTALLATION_COLUMNS} FROM marketplace_installations{''.join(clauses)} "
                "ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [installation_dict(row) for row in rows]

    def update_installation(
        self,
        tenant_id: object,
        installation_id: object,
        *,
        status: str | None = None,
        template_version_id: object = None,
        workflow_id: object = None,
        installed_version_id: object = None,
        job_id: object = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        expected_revision: int | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status is not None and status not in INSTALLATION_STATUSES:
            raise WorkBuddyMarketplaceError(
                ERROR_INVALID_ARGUMENT, "installation status is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        assignments = ["updated_at = now()", "revision = revision + 1"]
        params: list[object] = []
        for column, value in (
            ("status", status),
            ("error_code", error_code),
            ("error_detail", error_detail),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        for column, reference in (
            ("template_version_id", template_version_id),
            ("workflow_id", workflow_id),
            ("installed_version_id", installed_version_id),
            ("job_id", job_id),
        ):
            if reference is not None:
                assignments.append(f"{column} = ?")
                params.append(normalize_uuid(reference, field=column))
        params.extend([identifier, installation])
        clause = " WHERE tenant_id = ? AND id = ?"
        if expected_revision is not None:
            clause += " AND revision = ?"
            params.append(int(expected_revision))
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"UPDATE marketplace_installations SET {', '.join(assignments)}{clause} "
                f"RETURNING {_INSTALLATION_COLUMNS}",
                tuple(params),
            ).fetchone()
            if row is None:
                exists = active.execute(
                    "SELECT revision FROM marketplace_installations WHERE tenant_id = ? AND id = ?",
                    (identifier, installation),
                ).fetchone()
                if exists is None:
                    raise WorkBuddyMarketplaceError(
                        ERROR_MARKETPLACE_NOT_FOUND, "installation not found"
                    )
                raise WorkBuddyMarketplaceError(
                    ERROR_CONFLICT, "the installation changed since it was read"
                )
        return installation_dict(row)

    def record_credential_bindings(
        self,
        tenant_id: object,
        installation_id: object,
        bindings: Mapping[str, str],
        *,
        ctx: Any = None,
        conn: Any = None,
    ) -> list[dict[str, Any]]:
        """Persist the same-tenant credential binding for each declared slot."""
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        context = self._tenant_context(identifier, ctx=ctx)
        rows: list[DbRow] = []
        with _db_errors(code=ERROR_INVALID_ARGUMENT), self._transaction(context, conn) as active:
            for slot, credential in bindings.items():
                row = active.execute(
                    "INSERT INTO installation_credential_bindings("
                    "tenant_id, installation_id, binding_key, credential_id) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (tenant_id, installation_id, binding_key) DO UPDATE "
                    "SET credential_id = EXCLUDED.credential_id "
                    "RETURNING binding_key, credential_id, created_at",
                    (
                        identifier,
                        installation,
                        str(slot),
                        normalize_uuid(credential, field="credential_id"),
                    ),
                ).fetchone()
                if row is not None:
                    rows.append(row)
        return [binding_dict(row) for row in rows]

    def list_credential_bindings(
        self, tenant_id: object, installation_id: object, *, ctx: Any = None
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                "SELECT binding_key, credential_id, created_at FROM installation_credential_bindings "
                "WHERE tenant_id = ? AND installation_id = ? ORDER BY binding_key",
                (identifier, installation),
            ).fetchall()
        return [binding_dict(row) for row in rows]

    def record_consent(
        self,
        tenant_id: object,
        *,
        installation_id: object,
        subject_kind: str,
        subject_id: object,
        template_id: object,
        template_version_id: object,
        license_id: object,
        license_text_hash: str,
        capabilities: Sequence[Mapping[str, Any]],
        capabilities_hash: str,
        consented_by: object,
        consented_by_user_id: int | None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Append one consent record (append-only ledger)."""
        if subject_kind not in CONSENT_SUBJECTS:
            raise WorkBuddyMarketplaceError(
                ERROR_INVALID_ARGUMENT, "consent subject is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        context = self._tenant_context(identifier, user_id=consented_by_user_id, ctx=ctx)
        with _db_errors(code=ERROR_INVALID_ARGUMENT), self._transaction(context, conn) as active:
            row = active.execute(
                "INSERT INTO marketplace_installation_consents("
                "tenant_id, installation_id, subject_kind, subject_id, template_id, "
                "template_version_id, license_id, license_text_hash, capabilities, "
                "capabilities_hash, consented_by, consented_by_user_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?) "
                "RETURNING id, installation_id, subject_kind, subject_id, template_id, "
                "template_version_id, license_id, license_text_hash, capabilities, "
                "capabilities_hash, consented_by, consented_by_user_id, consented_at",
                (
                    identifier,
                    normalize_uuid(installation_id, field="installation_id"),
                    subject_kind,
                    normalize_uuid(subject_id, field="subject_id"),
                    normalize_uuid(template_id, field="template_id"),
                    normalize_uuid(template_version_id, field="template_version_id"),
                    str(license_id),
                    str(license_text_hash),
                    _jsonb(list(capabilities)),
                    str(capabilities_hash),
                    normalize_uuid(consented_by, field="consented_by"),
                    consented_by_user_id,
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyMarketplaceError(
                ERROR_DEPENDENCY_UNAVAILABLE, "consent row was not created"
            )
        return consent_dict(row)

    def list_consents(
        self, tenant_id: object, installation_id: object, *, ctx: Any = None
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                "SELECT id, installation_id, subject_kind, subject_id, template_id, "
                "template_version_id, license_id, license_text_hash, capabilities, "
                "capabilities_hash, consented_by, consented_by_user_id, consented_at "
                "FROM marketplace_installation_consents "
                "WHERE tenant_id = ? AND installation_id = ? ORDER BY consented_at DESC, id",
                (identifier, installation),
            ).fetchall()
        return [consent_dict(row) for row in rows]

    # ── upgrades (tenant) ────────────────────────────────────────────────────

    def create_upgrade(
        self,
        tenant_id: object,
        *,
        installation_id: object,
        from_template_version_id: object,
        to_template_version_id: object,
        workflow_id: object,
        requested_by: object,
        requested_by_user_id: int | None,
        status: str = "pending",
        job_id: object = None,
        consented_license_hash: str | None = None,
        consented_capabilities: Sequence[Mapping[str, Any]] | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status not in UPGRADE_STATUSES:
            raise WorkBuddyMarketplaceError(
                ERROR_INVALID_ARGUMENT, "upgrade status is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        context = self._tenant_context(identifier, user_id=requested_by_user_id, ctx=ctx)
        capabilities_json = (
            None if consented_capabilities is None else _jsonb(list(consented_capabilities))
        )
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                "INSERT INTO marketplace_upgrades("
                "tenant_id, installation_id, from_template_version_id, to_template_version_id, "
                "workflow_id, requested_by, requested_by_user_id, status, job_id, "
                "consented_license_hash, consented_capabilities, consented_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "CASE WHEN ?::jsonb IS NULL THEN NULL ELSE ?::jsonb END, "
                "CASE WHEN ?::jsonb IS NULL THEN NULL ELSE now() END) "
                f"RETURNING {_UPGRADE_COLUMNS}",
                (
                    identifier,
                    normalize_uuid(installation_id, field="installation_id"),
                    normalize_uuid(from_template_version_id, field="from_template_version_id"),
                    normalize_uuid(to_template_version_id, field="to_template_version_id"),
                    normalize_uuid(workflow_id, field="workflow_id"),
                    normalize_uuid(requested_by, field="requested_by"),
                    requested_by_user_id,
                    status,
                    None if job_id is None else str(job_id),
                    consented_license_hash,
                    capabilities_json,
                    capabilities_json,
                    capabilities_json,
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyMarketplaceError(
                ERROR_DEPENDENCY_UNAVAILABLE, "upgrade row was not created"
            )
        return upgrade_dict(row)

    def get_upgrade(
        self, tenant_id: object, upgrade_id: object, *, ctx: Any = None
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        upgrade = normalize_uuid(upgrade_id, field="upgrade_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            row = active.execute(
                f"SELECT {_UPGRADE_COLUMNS} FROM marketplace_upgrades "
                "WHERE tenant_id = ? AND id = ?",
                (identifier, upgrade),
            ).fetchone()
        return upgrade_dict(row) if row else None

    def list_upgrades(
        self, tenant_id: object, installation_id: object, *, ctx: Any = None
    ) -> list[dict[str, Any]]:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        installation = normalize_uuid(installation_id, field="installation_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            rows = active.execute(
                f"SELECT {_UPGRADE_COLUMNS} FROM marketplace_upgrades "
                "WHERE tenant_id = ? AND installation_id = ? ORDER BY created_at DESC, id",
                (identifier, installation),
            ).fetchall()
        return [upgrade_dict(row) for row in rows]

    def update_upgrade(
        self,
        tenant_id: object,
        upgrade_id: object,
        *,
        status: str | None = None,
        workflow_version_id: object = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status is not None and status not in UPGRADE_STATUSES:
            raise WorkBuddyMarketplaceError(
                ERROR_INVALID_ARGUMENT, "upgrade status is not supported"
            )
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        upgrade = normalize_uuid(upgrade_id, field="upgrade_id")
        assignments = ["updated_at = now()"]
        params: list[object] = []
        for column, value in (
            ("status", status),
            ("error_code", error_code),
            ("error_detail", error_detail),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                params.append(value)
        if workflow_version_id is not None:
            assignments.append("workflow_version_id = ?")
            params.append(normalize_uuid(workflow_version_id, field="workflow_version_id"))
        params.extend([identifier, upgrade])
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                f"UPDATE marketplace_upgrades SET {', '.join(assignments)} "
                f"WHERE tenant_id = ? AND id = ? RETURNING {_UPGRADE_COLUMNS}",
                tuple(params),
            ).fetchone()
            if row is None:
                raise WorkBuddyMarketplaceError(ERROR_MARKETPLACE_NOT_FOUND, "upgrade not found")
        return upgrade_dict(row)

    # ── marketplace jobs (shared workbuddy_jobs table) ───────────────────────

    def create_job(
        self,
        tenant_id: object,
        *,
        kind: str,
        requested_by_user_id: int | None,
        request_payload: Mapping[str, Any],
        idempotency_key: str | None = None,
        status: str = "queued",
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise WorkBuddyMarketplaceError(ERROR_INVALID_ARGUMENT, "job status is not supported")
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        context = self._tenant_context(identifier, user_id=requested_by_user_id, ctx=ctx)
        job_id = new_id()
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                "INSERT INTO workbuddy_jobs("
                "id, tenant_id, kind, status, progress, requested_by_user_id, idempotency_key, result"
                ") VALUES (?, ?, ?, ?, 0, ?, ?, ?::jsonb) "
                f"RETURNING {_JOB_COLUMNS}",
                (
                    job_id,
                    identifier,
                    kind,
                    status,
                    requested_by_user_id,
                    idempotency_key,
                    _jsonb(dict(request_payload)),
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyMarketplaceError(ERROR_DEPENDENCY_UNAVAILABLE, "job row was not created")
        return job_dict(row)

    def get_job(
        self, tenant_id: object, job_id: object, *, ctx: Any = None
    ) -> dict[str, Any] | None:
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        job = normalize_uuid(job_id, field="job_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, None) as active:
            row = active.execute(
                f"SELECT {_JOB_COLUMNS} FROM workbuddy_jobs WHERE tenant_id = ? AND id = ?",
                (identifier, job),
            ).fetchone()
        return job_dict(row) if row else None

    def update_job(
        self,
        tenant_id: object,
        job_id: object,
        *,
        status: str,
        progress: int = 0,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        ctx: Any = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise WorkBuddyMarketplaceError(ERROR_INVALID_ARGUMENT, "job status is not supported")
        identifier = normalize_uuid(tenant_id, field="tenant_id")
        job = normalize_uuid(job_id, field="job_id")
        context = self._tenant_context(identifier, ctx=ctx)
        with _db_errors(), self._transaction(context, conn) as active:
            row = active.execute(
                "UPDATE workbuddy_jobs SET status = ?, progress = ?, "
                "result = CASE WHEN ?::jsonb IS NULL THEN result ELSE ?::jsonb END, "
                "error_code = ?, error_message = ?, "
                "started_at = CASE WHEN ? = 'running' THEN coalesce(started_at, now()) ELSE started_at END, "
                "finished_at = CASE WHEN ? IN ('succeeded', 'failed', 'cancelled') THEN now() ELSE finished_at END "
                f"WHERE tenant_id = ? AND id = ? RETURNING {_JOB_COLUMNS}",
                (
                    status,
                    max(0, min(int(progress), 100)),
                    None if result is None else _jsonb(dict(result)),
                    None if result is None else _jsonb(dict(result)),
                    error_code,
                    None if error_message is None else str(error_message)[:500],
                    status,
                    status,
                    identifier,
                    job,
                ),
            ).fetchone()
            if row is None:
                raise WorkBuddyMarketplaceError(ERROR_MARKETPLACE_NOT_FOUND, "job not found")
        return job_dict(row)

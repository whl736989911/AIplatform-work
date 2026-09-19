"""WorkBuddy connector-credential governance and platform catalog access.

PostgreSQL only: every method runs inside
:func:`octop.infra.db.workbuddy_context.workbuddy_transaction`, which fails
closed with ``WorkBuddyPostgresRequiredError`` on SQLite before a statement is
sent.

Guarantees this module keeps:

* raw credential material is never accepted or persisted.  Callers hand in a
  server-generated ``external_ref`` pointer (``scheme://path``) and only metadata
  is stored, so metadata projections cannot leak a secret;
* credential revisions are an append-only ledger keyed ``(credential_id,
  revision)``; the table triggers reject rewrites and deletes;
* platform tool and model rows are fixed revisions identified only by
  ``adapter_key`` plus ``tool_key``/``model_key`` plus display metadata.  A key
  with a live revision refuses re-publication, and revocation is one-way;
* tenant capabilities may only reference published revisions, and the tenant
  default model must stay inside the tenant's approved model set;
* a cross-tenant or unknown object id is reported as ``None``/``False`` — never
  as an existence detail.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard, TypeVar

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import UNSET, DbRow, now_ts, sql_in_placeholders
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction

__all__ = [
    "CODE_CAPABILITY_NO_FIELDS",
    "CODE_CAPABILITY_NOT_APPROVED",
    "CODE_CAPABILITY_REVISION_CONFLICT",
    "CODE_CATALOG_ERROR",
    "CODE_CREDENTIAL_NAME_TAKEN",
    "CODE_CREDENTIAL_REF_INVALID",
    "CODE_CREDENTIAL_REVISION_CONFLICT",
    "CODE_CREDENTIAL_REVOKED",
    "CODE_INVALID_INPUT",
    "CODE_MEMBERSHIP_REQUIRED",
    "CODE_PLATFORM_REVISION_CONFLICT",
    "CODE_PLATFORM_REVISION_REVOKED",
    "CODE_REVISION_CONFLICT",
    "CODE_REVISION_REVOKED",
    "STATUS_ACTIVE",
    "STATUS_PUBLISHED",
    "STATUS_REVOKED",
    "UNSET",
    "WorkBuddyCapabilities",
    "WorkBuddyCapabilityNotApproved",
    "WorkBuddyCasConflict",
    "WorkBuddyCatalogError",
    "WorkBuddyCatalogRepo",
    "WorkBuddyCredential",
    "WorkBuddyCredentialNameTaken",
    "WorkBuddyCredentialRevision",
    "WorkBuddyGrant",
    "WorkBuddyInvalidInput",
    "WorkBuddyMembershipRequired",
    "WorkBuddyModelRevision",
    "WorkBuddyPlatformRevisionConflict",
    "WorkBuddyRevisionRevoked",
    "WorkBuddyToolRevision",
]

STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_PUBLISHED = "published"

ACTION_CREATED = "created"
ACTION_ROTATED = "rotated"
ACTION_REVOKED = "revoked"

CODE_CATALOG_ERROR = "WORKBUDDY_CATALOG_ERROR"
CODE_INVALID_INPUT = "WORKBUDDY_INVALID_INPUT"
CODE_CREDENTIAL_REF_INVALID = "WORKBUDDY_CREDENTIAL_REF_INVALID"
CODE_CREDENTIAL_NAME_TAKEN = "WORKBUDDY_CREDENTIAL_NAME_TAKEN"
CODE_CREDENTIAL_REVOKED = "WORKBUDDY_CREDENTIAL_REVOKED"
CODE_CREDENTIAL_REVISION_CONFLICT = "WORKBUDDY_CREDENTIAL_REVISION_CONFLICT"
CODE_REVISION_CONFLICT = "WORKBUDDY_REVISION_CONFLICT"
CODE_REVISION_REVOKED = "WORKBUDDY_REVISION_REVOKED"
CODE_PLATFORM_REVISION_CONFLICT = "WORKBUDDY_PLATFORM_REVISION_CONFLICT"
CODE_PLATFORM_REVISION_REVOKED = "WORKBUDDY_PLATFORM_REVISION_REVOKED"
CODE_CAPABILITY_NOT_APPROVED = "FORBIDDEN_ROLE"
CODE_CAPABILITY_REVISION_CONFLICT = "WORKBUDDY_CAPABILITY_REVISION_CONFLICT"
CODE_CAPABILITY_NO_FIELDS = "WORKBUDDY_CAPABILITY_NO_FIELDS"
CODE_MEMBERSHIP_REQUIRED = "FORBIDDEN_ROLE"

_TABLE_CREDENTIALS = "workbuddy_connector_credentials"
_TABLE_CREDENTIAL_REVISIONS = "workbuddy_connector_credential_revisions"
_TABLE_CREDENTIAL_GRANTS = "workbuddy_connector_credential_grants"
_TABLE_TOOL_REVISIONS = "workbuddy_platform_tool_revisions"
_TABLE_MODEL_REVISIONS = "workbuddy_platform_model_revisions"
_TABLE_CAPABILITIES = "workbuddy_tenant_capabilities"
_TABLE_TOOL_GRANTS = "workbuddy_tenant_tool_grants"
_TABLE_MODEL_GRANTS = "workbuddy_tenant_model_grants"
_TABLE_MEMBERS = "workbuddy_tenant_members"

_COL_TOOL_REVISION = "tool_revision_id"
_COL_MODEL_REVISION = "model_revision_id"

# A server-generated pointer such as ``vault://tenant/credential/rotation``.
_EXTERNAL_REF_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://\S{1,400}$")
_MAX_REF_LENGTH = 512
_MAX_DESCRIPTION_LENGTH = 1000

_PlatformRevision = TypeVar("_PlatformRevision", "WorkBuddyToolRevision", "WorkBuddyModelRevision")


class WorkBuddyCatalogError(Exception):
    """Base failure for catalog operations.

    ``code`` is a stable English identifier; routers map it to an HTTP status
    without parsing ``message``.
    """

    code = CODE_CATALOG_ERROR

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        self.message = message or self.code
        super().__init__(self.message)


class WorkBuddyInvalidInput(WorkBuddyCatalogError):
    """Caller supplied a value the schema and API contract cannot accept."""

    code = CODE_INVALID_INPUT


class WorkBuddyCredentialNameTaken(WorkBuddyCatalogError):
    """Another credential in the same tenant already uses that name."""

    code = CODE_CREDENTIAL_NAME_TAKEN


class WorkBuddyCasConflict(WorkBuddyCatalogError):
    """The expected revision did not match the stored revision."""

    code = CODE_REVISION_CONFLICT


class WorkBuddyRevisionRevoked(WorkBuddyCatalogError):
    """The target object or revision is already revoked."""

    code = CODE_REVISION_REVOKED


class WorkBuddyPlatformRevisionConflict(WorkBuddyCatalogError):
    """A live platform revision already exists for that adapter key pair."""

    code = CODE_PLATFORM_REVISION_CONFLICT


class WorkBuddyCapabilityNotApproved(WorkBuddyCatalogError):
    """A capability reference is unknown, revoked, or outside the tenant set."""

    code = CODE_CAPABILITY_NOT_APPROVED


class WorkBuddyMembershipRequired(WorkBuddyCatalogError):
    """The acting or targeted membership is not active in that tenant."""

    code = CODE_MEMBERSHIP_REQUIRED


@dataclass(frozen=True)
class WorkBuddyCredential:
    """Connector credential metadata for one tenant (never secret material)."""

    credential_id: str
    tenant_id: str
    name: str
    connector_kind: str
    description: str
    scopes: tuple[str, ...]
    status: str
    revision: int
    external_ref: str
    owner_member_id: str
    created_at: int
    updated_at: int
    revoked_at: int | None
    revoked_by_member_id: str | None

    @property
    def revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyCredential:
        return cls(
            credential_id=str(row["credential_id"]),
            tenant_id=str(row["tenant_id"]),
            name=str(row["name"]),
            connector_kind=str(row["connector_kind"]),
            description=str(row["description"]),
            scopes=_json_text_tuple(row["scopes"]),
            status=str(row["status"]),
            revision=int(row["revision"]),
            external_ref=str(row["external_ref"]),
            owner_member_id=str(row["owner_membership_id"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            revoked_at=_optional_int(row["revoked_at"]),
            revoked_by_member_id=_optional_str(row["revoked_by_membership_id"]),
        )


@dataclass(frozen=True)
class WorkBuddyCredentialRevision:
    """One append-only ledger entry for a credential write."""

    credential_revision_id: str
    tenant_id: str
    credential_id: str
    revision: int
    action: str
    external_ref: str
    actor_member_id: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyCredentialRevision:
        return cls(
            credential_revision_id=str(row["credential_revision_id"]),
            tenant_id=str(row["tenant_id"]),
            credential_id=str(row["credential_id"]),
            revision=int(row["revision"]),
            action=str(row["action"]),
            external_ref=str(row["external_ref"]),
            actor_member_id=str(row["actor_membership_id"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True)
class WorkBuddyGrant:
    """A member of the owning tenant that may use one credential."""

    grant_id: str
    tenant_id: str
    credential_id: str
    member_id: str
    granted_by_member_id: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyGrant:
        return cls(
            grant_id=str(row["grant_id"]),
            tenant_id=str(row["tenant_id"]),
            credential_id=str(row["credential_id"]),
            member_id=str(row["membership_id"]),
            granted_by_member_id=str(row["granted_by_membership_id"]),
            created_at=int(row["granted_at"]),
        )


@dataclass(frozen=True)
class WorkBuddyToolRevision:
    """An immutable platform tool revision."""

    tool_revision_id: str
    adapter_key: str
    tool_key: str
    revision: int
    display_name: str
    description: str
    status: str
    published_by_user_id: int
    published_at: int
    revoked_by_user_id: int | None
    revoked_at: int | None

    @property
    def revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyToolRevision:
        return cls(
            tool_revision_id=str(row["tool_revision_id"]),
            adapter_key=str(row["adapter_key"]),
            tool_key=str(row["tool_key"]),
            revision=int(row["revision"]),
            display_name=str(row["display_name"]),
            description=str(row["description"]),
            status=str(row["status"]),
            published_by_user_id=int(row["published_by_user_id"]),
            published_at=int(row["published_at"]),
            revoked_by_user_id=_optional_int(row["revoked_by_user_id"]),
            revoked_at=_optional_int(row["revoked_at"]),
        )


@dataclass(frozen=True)
class WorkBuddyModelRevision:
    """An immutable platform model revision."""

    model_revision_id: str
    adapter_key: str
    model_key: str
    revision: int
    display_name: str
    description: str
    status: str
    published_by_user_id: int
    published_at: int
    revoked_by_user_id: int | None
    revoked_at: int | None

    @property
    def revoked(self) -> bool:
        return self.status == STATUS_REVOKED

    @classmethod
    def from_row(cls, row: DbRow) -> WorkBuddyModelRevision:
        return cls(
            model_revision_id=str(row["model_revision_id"]),
            adapter_key=str(row["adapter_key"]),
            model_key=str(row["model_key"]),
            revision=int(row["revision"]),
            display_name=str(row["display_name"]),
            description=str(row["description"]),
            status=str(row["status"]),
            published_by_user_id=int(row["published_by_user_id"]),
            published_at=int(row["published_at"]),
            revoked_by_user_id=_optional_int(row["revoked_by_user_id"]),
            revoked_at=_optional_int(row["revoked_at"]),
        )


@dataclass(frozen=True)
class WorkBuddyCapabilities:
    """A tenant's approved tool/model revisions plus its default model."""

    tenant_id: str
    revision: int
    tool_revision_ids: tuple[str, ...]
    model_revision_ids: tuple[str, ...]
    default_model_revision_id: str | None
    updated_at: int
    updated_by_member_id: str | None

    @classmethod
    def from_row(
        cls,
        row: DbRow,
        *,
        tool_revision_ids: Sequence[str],
        model_revision_ids: Sequence[str],
    ) -> WorkBuddyCapabilities:
        return cls(
            tenant_id=str(row["tenant_id"]),
            revision=int(row["revision"]),
            tool_revision_ids=tuple(str(v) for v in tool_revision_ids),
            model_revision_ids=tuple(str(v) for v in model_revision_ids),
            default_model_revision_id=_optional_str(row["default_model_revision_id"]),
            updated_at=int(row["updated_at"]),
            updated_by_member_id=_optional_str(row["updated_by_membership_id"]),
        )


class WorkBuddyCatalogRepo:
    """Connector credential metadata, platform catalog, and tenant capabilities.

    Constructor takes the shared database pool (``server.services.db``); the
    repository opens its own tenant- or platform-scoped transaction per call and
    never touches another tenant's rows.
    """

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # ── connector credential metadata ──────────────────────────────────────

    def list_credentials(self, tenant_id: str) -> list[WorkBuddyCredential]:
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_CREDENTIALS} WHERE tenant_id = ? "
                "ORDER BY name, credential_id",
                (tenant_id,),
            ).fetchall()
        return [WorkBuddyCredential.from_row(row) for row in rows]

    def get_credential(self, tenant_id: str, credential_id: str) -> WorkBuddyCredential | None:
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            row = _credential_row(conn, tenant_id, credential_id)
        return WorkBuddyCredential.from_row(row) if row is not None else None

    def create_credential(
        self,
        tenant_id: str,
        *,
        name: str,
        connector_kind: str,
        external_ref: str,
        actor_member_id: str,
        description: str = "",
        scopes: Sequence[str] = (),
    ) -> WorkBuddyCredential:
        """Register credential metadata; ``external_ref`` is a pointer, not a secret."""
        clean_name = _validated_text(name, field="name", max_length=120)
        clean_kind = _validated_text(connector_kind, field="connector_kind", max_length=120)
        clean_ref = _validated_external_ref(external_ref)
        clean_description = _validated_description(description)
        scopes_json = json.dumps([str(scope) for scope in scopes])
        ts = now_ts()
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            _require_active_member(conn, tenant_id, actor_member_id)
            taken = conn.execute(
                f"SELECT 1 FROM {_TABLE_CREDENTIALS} WHERE tenant_id = ? AND name = ?",
                (tenant_id, clean_name),
            ).fetchone()
            if taken is not None:
                raise WorkBuddyCredentialNameTaken(
                    f"credential name is already used in this tenant: {clean_name}"
                )
            row = conn.execute(
                f"INSERT INTO {_TABLE_CREDENTIALS}("
                " tenant_id, name, connector_kind, description, scopes, status, revision,"
                " external_ref, owner_membership_id, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?) RETURNING *",
                (
                    tenant_id,
                    clean_name,
                    clean_kind,
                    clean_description,
                    scopes_json,
                    clean_ref,
                    actor_member_id,
                    ts,
                    ts,
                ),
            ).fetchone()
            if row is None:
                raise WorkBuddyCatalogError("credential insert returned no row")
            conn.execute(
                f"INSERT INTO {_TABLE_CREDENTIAL_REVISIONS}("
                " tenant_id, credential_id, revision, action, external_ref,"
                " actor_membership_id, created_at"
                ") VALUES (?, ?, 1, ?, ?, ?, ?)",
                (tenant_id, row["credential_id"], ACTION_CREATED, clean_ref, actor_member_id, ts),
            )
        return WorkBuddyCredential.from_row(row)

    def rotate_credential(
        self,
        tenant_id: str,
        credential_id: str,
        *,
        external_ref: str,
        actor_member_id: str,
        expected_revision: int | None = None,
    ) -> WorkBuddyCredential | None:
        """Point the credential at a new secret revision; unknown id yields ``None``."""
        clean_ref = _validated_external_ref(external_ref)
        ts = now_ts()
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            row = _credential_row(conn, tenant_id, credential_id, lock=True)
            if row is None:
                return None
            if str(row["status"]) == STATUS_REVOKED:
                raise WorkBuddyRevisionRevoked(
                    "credential is revoked and cannot be rotated", code=CODE_CREDENTIAL_REVOKED
                )
            _require_active_member(conn, tenant_id, actor_member_id)
            current_revision = int(row["revision"])
            if expected_revision is not None and int(expected_revision) != current_revision:
                raise WorkBuddyCasConflict(
                    "credential revision conflict", code=CODE_CREDENTIAL_REVISION_CONFLICT
                )
            updated = conn.execute(
                f"UPDATE {_TABLE_CREDENTIALS}"
                " SET external_ref = ?, revision = revision + 1, updated_at = ?"
                " WHERE tenant_id = ? AND credential_id = ? AND revision = ? RETURNING *",
                (clean_ref, ts, tenant_id, credential_id, current_revision),
            ).fetchone()
            if updated is None:
                raise WorkBuddyCasConflict(
                    "credential revision conflict", code=CODE_CREDENTIAL_REVISION_CONFLICT
                )
            conn.execute(
                f"INSERT INTO {_TABLE_CREDENTIAL_REVISIONS}("
                " tenant_id, credential_id, revision, action, external_ref,"
                " actor_membership_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    credential_id,
                    int(updated["revision"]),
                    ACTION_ROTATED,
                    clean_ref,
                    actor_member_id,
                    ts,
                ),
            )
        return WorkBuddyCredential.from_row(updated)

    def revoke_credential(
        self, tenant_id: str, credential_id: str, *, actor_member_id: str
    ) -> WorkBuddyCredential | None:
        """Revoke a credential; unknown id yields ``None`` and repeats are no-ops."""
        ts = now_ts()
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            row = _credential_row(conn, tenant_id, credential_id, lock=True)
            if row is None:
                return None
            if str(row["status"]) == STATUS_REVOKED:
                return WorkBuddyCredential.from_row(row)
            _require_active_member(conn, tenant_id, actor_member_id)
            updated = conn.execute(
                f"UPDATE {_TABLE_CREDENTIALS}"
                " SET status = 'revoked', revoked_at = ?, revoked_by_membership_id = ?,"
                " revision = revision + 1, updated_at = ?"
                " WHERE tenant_id = ? AND credential_id = ? AND status = 'active' RETURNING *",
                (ts, actor_member_id, ts, tenant_id, credential_id),
            ).fetchone()
            if updated is None:
                return WorkBuddyCredential.from_row(row)
            conn.execute(
                f"INSERT INTO {_TABLE_CREDENTIAL_REVISIONS}("
                " tenant_id, credential_id, revision, action, external_ref,"
                " actor_membership_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    credential_id,
                    int(updated["revision"]),
                    ACTION_REVOKED,
                    str(updated["external_ref"]),
                    actor_member_id,
                    ts,
                ),
            )
        return WorkBuddyCredential.from_row(updated)

    def list_credential_revisions(
        self, tenant_id: str, credential_id: str
    ) -> list[WorkBuddyCredentialRevision]:
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_CREDENTIAL_REVISIONS}"
                " WHERE tenant_id = ? AND credential_id = ? ORDER BY revision",
                (tenant_id, credential_id),
            ).fetchall()
        return [WorkBuddyCredentialRevision.from_row(row) for row in rows]

    # ── credential grants ──────────────────────────────────────────────────

    def list_grants(self, tenant_id: str, credential_id: str) -> list[WorkBuddyGrant]:
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_CREDENTIAL_GRANTS}"
                " WHERE tenant_id = ? AND credential_id = ? ORDER BY granted_at, membership_id",
                (tenant_id, credential_id),
            ).fetchall()
        return [WorkBuddyGrant.from_row(row) for row in rows]

    def create_grant(
        self,
        tenant_id: str,
        credential_id: str,
        *,
        target_member_id: str,
        actor_member_id: str,
    ) -> WorkBuddyGrant | None:
        """Grant one tenant member access; unknown credential/member yields ``None``."""
        ts = now_ts()
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            credential = _credential_row(conn, tenant_id, credential_id)
            if credential is None:
                return None
            if str(credential["status"]) == STATUS_REVOKED:
                raise WorkBuddyRevisionRevoked(
                    "credential is revoked and cannot be granted", code=CODE_CREDENTIAL_REVOKED
                )
            _require_active_member(conn, tenant_id, actor_member_id)
            if not _is_active_member(conn, tenant_id, target_member_id):
                return None
            row = conn.execute(
                f"INSERT INTO {_TABLE_CREDENTIAL_GRANTS}("
                " tenant_id, credential_id, membership_id, granted_by_membership_id, granted_at"
                ") VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (tenant_id, credential_id, membership_id) DO NOTHING RETURNING *",
                (tenant_id, credential_id, target_member_id, actor_member_id, ts),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"SELECT * FROM {_TABLE_CREDENTIAL_GRANTS}"
                    " WHERE tenant_id = ? AND credential_id = ? AND membership_id = ?",
                    (tenant_id, credential_id, target_member_id),
                ).fetchone()
        return WorkBuddyGrant.from_row(row) if row is not None else None

    def delete_grant(self, tenant_id: str, credential_id: str, target_member_id: str) -> bool:
        """Drop one grant; ``False`` when nothing matched."""
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            row = conn.execute(
                f"DELETE FROM {_TABLE_CREDENTIAL_GRANTS}"
                " WHERE tenant_id = ? AND credential_id = ? AND membership_id = ?"
                " RETURNING grant_id",
                (tenant_id, credential_id, target_member_id),
            ).fetchone()
        return row is not None

    # ── platform catalog: tool revisions ───────────────────────────────────

    def publish_tool(
        self,
        *,
        adapter_key: str,
        tool_key: str,
        display_name: str,
        actor_user_id: int,
        description: str = "",
    ) -> WorkBuddyToolRevision:
        """Freeze a new platform tool revision for ``(adapter_key, tool_key)``."""
        return self._publish_revision(
            table=_TABLE_TOOL_REVISIONS,
            key_column="tool_key",
            key_value=tool_key,
            adapter_key=adapter_key,
            display_name=display_name,
            description=description,
            actor_user_id=actor_user_id,
            record=WorkBuddyToolRevision,
        )

    def get_tool_revision(self, tool_revision_id: str) -> WorkBuddyToolRevision | None:
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            row = conn.execute(
                f"SELECT * FROM {_TABLE_TOOL_REVISIONS} WHERE {_COL_TOOL_REVISION} = ?",
                (tool_revision_id,),
            ).fetchone()
        return WorkBuddyToolRevision.from_row(row) if row is not None else None

    def list_public_tools(self) -> list[WorkBuddyToolRevision]:
        """Newest published revision per key; a revoked newest revision hides the key."""
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            rows = conn.execute(
                f"SELECT * FROM ("
                f" SELECT DISTINCT ON (adapter_key, tool_key) * FROM {_TABLE_TOOL_REVISIONS}"
                " ORDER BY adapter_key, tool_key, revision DESC"
                ") latest WHERE status = 'published' ORDER BY adapter_key, tool_key"
            ).fetchall()
        return [WorkBuddyToolRevision.from_row(row) for row in rows]

    def list_tool_revisions(self) -> list[WorkBuddyToolRevision]:
        """Every platform tool revision, revoked ones included (admin view)."""
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_TOOL_REVISIONS}"
                " ORDER BY adapter_key, tool_key, revision DESC"
            ).fetchall()
        return [WorkBuddyToolRevision.from_row(row) for row in rows]

    def revoke_tool(self, tool_revision_id: str, *, actor_user_id: int) -> bool:
        """Revoke one tool revision and every tenant grant that referenced it."""
        return self._revoke_revision(
            table=_TABLE_TOOL_REVISIONS,
            pk_column=_COL_TOOL_REVISION,
            grant_table=_TABLE_TOOL_GRANTS,
            grant_column=_COL_TOOL_REVISION,
            revision_id=tool_revision_id,
            actor_user_id=actor_user_id,
        )

    # ── platform catalog: model revisions ──────────────────────────────────

    def publish_model(
        self,
        *,
        adapter_key: str,
        model_key: str,
        display_name: str,
        actor_user_id: int,
        description: str = "",
    ) -> WorkBuddyModelRevision:
        """Freeze a new platform model revision for ``(adapter_key, model_key)``."""
        return self._publish_revision(
            table=_TABLE_MODEL_REVISIONS,
            key_column="model_key",
            key_value=model_key,
            adapter_key=adapter_key,
            display_name=display_name,
            description=description,
            actor_user_id=actor_user_id,
            record=WorkBuddyModelRevision,
        )

    def get_model_revision(self, model_revision_id: str) -> WorkBuddyModelRevision | None:
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            row = conn.execute(
                f"SELECT * FROM {_TABLE_MODEL_REVISIONS} WHERE {_COL_MODEL_REVISION} = ?",
                (model_revision_id,),
            ).fetchone()
        return WorkBuddyModelRevision.from_row(row) if row is not None else None

    def list_public_models(self) -> list[WorkBuddyModelRevision]:
        """Newest published revision per key; a revoked newest revision hides the key."""
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            rows = conn.execute(
                f"SELECT * FROM ("
                f" SELECT DISTINCT ON (adapter_key, model_key) * FROM {_TABLE_MODEL_REVISIONS}"
                " ORDER BY adapter_key, model_key, revision DESC"
                ") latest WHERE status = 'published' ORDER BY adapter_key, model_key"
            ).fetchall()
        return [WorkBuddyModelRevision.from_row(row) for row in rows]

    def list_model_revisions(self) -> list[WorkBuddyModelRevision]:
        """Every platform model revision, revoked ones included (admin view)."""
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            rows = conn.execute(
                f"SELECT * FROM {_TABLE_MODEL_REVISIONS}"
                " ORDER BY adapter_key, model_key, revision DESC"
            ).fetchall()
        return [WorkBuddyModelRevision.from_row(row) for row in rows]

    def revoke_model(self, model_revision_id: str, *, actor_user_id: int) -> bool:
        """Revoke one model revision, its tenant grants, and any tenant default."""
        return self._revoke_revision(
            table=_TABLE_MODEL_REVISIONS,
            pk_column=_COL_MODEL_REVISION,
            grant_table=_TABLE_MODEL_GRANTS,
            grant_column=_COL_MODEL_REVISION,
            revision_id=model_revision_id,
            actor_user_id=actor_user_id,
        )

    # ── tenant capabilities ────────────────────────────────────────────────

    def get_capabilities(self, tenant_id: str) -> WorkBuddyCapabilities | None:
        """Current capability set, or ``None`` when the tenant never configured one."""
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            row = _capability_row(conn, tenant_id)
            if row is None:
                return None
            return _capabilities_record(conn, row)

    def update_capabilities(
        self,
        tenant_id: str,
        *,
        actor_member_id: str,
        tool_revision_ids: Iterable[str] | None | object = UNSET,
        model_revision_ids: Iterable[str] | None | object = UNSET,
        default_model_revision_id: str | None | object = UNSET,
        expected_revision: int | None = None,
    ) -> WorkBuddyCapabilities:
        """Replace approved tool/model revisions and the tenant default model.

        ``UNSET`` leaves a field untouched, an explicit ``None`` clears the
        default model, and an empty iterable clears that grant set.
        """
        if (
            tool_revision_ids is UNSET
            and model_revision_ids is UNSET
            and default_model_revision_id is UNSET
        ):
            raise WorkBuddyInvalidInput(
                "no capability fields supplied", code=CODE_CAPABILITY_NO_FIELDS
            )
        ts = now_ts()
        with workbuddy_transaction(self._db, _tenant_context(tenant_id)) as conn:
            _require_active_member(conn, tenant_id, actor_member_id)
            row = _capability_row(conn, tenant_id, lock=True)
            if expected_revision is not None:
                current_revision = int(row["revision"]) if row is not None else 0
                if int(expected_revision) != current_revision:
                    raise WorkBuddyCasConflict(
                        "capability revision conflict", code=CODE_CAPABILITY_REVISION_CONFLICT
                    )
            current_tools, current_models = _capability_grants(conn, tenant_id)
            existing_default = (
                _optional_str(row["default_model_revision_id"]) if row is not None else None
            )
            tools = (
                current_tools
                if not _is_id_collection(tool_revision_ids)
                else _normalized_ids(tool_revision_ids)
            )
            models = (
                current_models
                if not _is_id_collection(model_revision_ids)
                else _normalized_ids(model_revision_ids)
            )
            if default_model_revision_id is UNSET:
                default_id = existing_default
            else:
                default_id = _optional_str(default_model_revision_id)
            _require_published_revisions(conn, _TABLE_TOOL_REVISIONS, _COL_TOOL_REVISION, tools)
            _require_published_revisions(conn, _TABLE_MODEL_REVISIONS, _COL_MODEL_REVISION, models)
            if default_id is not None and default_id not in models:
                raise WorkBuddyCapabilityNotApproved(
                    "default model must be one of the approved model revisions"
                )
            if (
                row is not None
                and tools == current_tools
                and models == current_models
                and default_id == existing_default
            ):
                return _capabilities_record(conn, row)
            _sync_grant_set(
                conn,
                table=_TABLE_TOOL_GRANTS,
                column=_COL_TOOL_REVISION,
                tenant_id=tenant_id,
                current=current_tools,
                desired=tools,
                actor_member_id=actor_member_id,
                ts=ts,
            )
            _sync_grant_set(
                conn,
                table=_TABLE_MODEL_GRANTS,
                column=_COL_MODEL_REVISION,
                tenant_id=tenant_id,
                current=current_models,
                desired=models,
                actor_member_id=actor_member_id,
                ts=ts,
            )
            updated = conn.execute(
                f"INSERT INTO {_TABLE_CAPABILITIES}("
                " tenant_id, revision, default_model_revision_id, updated_at,"
                " updated_by_membership_id"
                ") VALUES (?, 1, ?, ?, ?)"
                " ON CONFLICT (tenant_id) DO UPDATE SET"
                f" revision = {_TABLE_CAPABILITIES}.revision + 1,"
                " default_model_revision_id = EXCLUDED.default_model_revision_id,"
                " updated_at = EXCLUDED.updated_at,"
                " updated_by_membership_id = EXCLUDED.updated_by_membership_id"
                " RETURNING *",
                (tenant_id, default_id, ts, actor_member_id),
            ).fetchone()
            if updated is None:
                raise WorkBuddyCatalogError("capability upsert returned no row")
            return _capabilities_record(conn, updated)

    # ── shared platform catalog internals ──────────────────────────────────

    def _publish_revision(
        self,
        *,
        table: str,
        key_column: str,
        key_value: str,
        adapter_key: str,
        display_name: str,
        description: str,
        actor_user_id: int,
        record: type[_PlatformRevision],
    ) -> _PlatformRevision:
        clean_adapter = _validated_text(adapter_key, field="adapter_key", max_length=120)
        clean_key = _validated_text(key_value, field=key_column, max_length=200)
        clean_display = _validated_text(display_name, field="display_name", max_length=200)
        clean_description = _validated_description(description)
        ts = now_ts()
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            latest = conn.execute(
                f"SELECT revision, status FROM {table}"
                f" WHERE adapter_key = ? AND {key_column} = ? ORDER BY revision DESC LIMIT 1",
                (clean_adapter, clean_key),
            ).fetchone()
            if latest is not None and str(latest["status"]) == STATUS_PUBLISHED:
                raise WorkBuddyPlatformRevisionConflict(
                    f"{key_column} already has a live revision: {clean_adapter}/{clean_key}"
                )
            revision = int(latest["revision"]) + 1 if latest is not None else 1
            row = conn.execute(
                f"INSERT INTO {table}("
                f" adapter_key, {key_column}, revision, display_name, description, status,"
                " published_by_user_id, published_at"
                ") VALUES (?, ?, ?, ?, ?, 'published', ?, ?) RETURNING *",
                (
                    clean_adapter,
                    clean_key,
                    revision,
                    clean_display,
                    clean_description,
                    int(actor_user_id),
                    ts,
                ),
            ).fetchone()
        if row is None:
            raise WorkBuddyCatalogError("platform revision insert returned no row")
        return record.from_row(row)

    def _revoke_revision(
        self,
        *,
        table: str,
        pk_column: str,
        grant_table: str,
        grant_column: str,
        revision_id: str,
        actor_user_id: int,
    ) -> bool:
        ts = now_ts()
        with workbuddy_transaction(self._db, _platform_context()) as conn:
            row = conn.execute(
                f"SELECT {pk_column}, status FROM {table} WHERE {pk_column} = ? FOR UPDATE",
                (revision_id,),
            ).fetchone()
            if row is None:
                return False
            if str(row["status"]) == STATUS_REVOKED:
                raise WorkBuddyRevisionRevoked(
                    "platform revision is already revoked", code=CODE_PLATFORM_REVISION_REVOKED
                )
            conn.execute(f"DELETE FROM {grant_table} WHERE {grant_column} = ?", (revision_id,))
            if grant_table == _TABLE_MODEL_GRANTS:
                # A revoked model may not stay a tenant default.
                conn.execute(
                    f"UPDATE {_TABLE_CAPABILITIES}"
                    " SET default_model_revision_id = NULL, revision = revision + 1,"
                    " updated_at = ? WHERE default_model_revision_id = ?",
                    (ts, revision_id),
                )
            conn.execute(
                f"UPDATE {table}"
                " SET status = 'revoked', revoked_at = ?, revoked_by_user_id = ?"
                " WHERE " + pk_column + " = ? AND status = 'published'",
                (ts, int(actor_user_id), revision_id),
            )
        return True


# ── helpers ────────────────────────────────────────────────────────────────


def _tenant_context(tenant_id: str) -> WorkBuddyDbContext:
    return WorkBuddyDbContext.for_tenant(tenant_id)


def _platform_context() -> WorkBuddyDbContext:
    return WorkBuddyDbContext.platform()


def _credential_row(
    conn: Any, tenant_id: str, credential_id: str, *, lock: bool = False
) -> DbRow | None:
    sql = f"SELECT * FROM {_TABLE_CREDENTIALS} WHERE tenant_id = ? AND credential_id = ?"
    if lock:
        sql += " FOR UPDATE"
    row: DbRow | None = conn.execute(sql, (tenant_id, credential_id)).fetchone()
    return row


def _capability_row(conn: Any, tenant_id: str, *, lock: bool = False) -> DbRow | None:
    sql = f"SELECT * FROM {_TABLE_CAPABILITIES} WHERE tenant_id = ?"
    if lock:
        sql += " FOR UPDATE"
    row: DbRow | None = conn.execute(sql, (tenant_id,)).fetchone()
    return row


def _capability_grants(conn: Any, tenant_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    tool_rows = conn.execute(
        f"SELECT {_COL_TOOL_REVISION} AS revision_id FROM {_TABLE_TOOL_GRANTS}"
        " WHERE tenant_id = ? ORDER BY revision_id",
        (tenant_id,),
    ).fetchall()
    model_rows = conn.execute(
        f"SELECT {_COL_MODEL_REVISION} AS revision_id FROM {_TABLE_MODEL_GRANTS}"
        " WHERE tenant_id = ? ORDER BY revision_id",
        (tenant_id,),
    ).fetchall()
    return (
        tuple(str(row["revision_id"]) for row in tool_rows),
        tuple(str(row["revision_id"]) for row in model_rows),
    )


def _capabilities_record(conn: Any, row: DbRow) -> WorkBuddyCapabilities:
    tools, models = _capability_grants(conn, str(row["tenant_id"]))
    return WorkBuddyCapabilities.from_row(row, tool_revision_ids=tools, model_revision_ids=models)


def _sync_grant_set(
    conn: Any,
    *,
    table: str,
    column: str,
    tenant_id: str,
    current: Sequence[str],
    desired: Sequence[str],
    actor_member_id: str,
    ts: int,
) -> None:
    current_set = set(current)
    desired_set = set(desired)
    for revision_id in current:
        if revision_id not in desired_set:
            conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = ? AND {column} = ?",
                (tenant_id, revision_id),
            )
    for revision_id in desired:
        if revision_id not in current_set:
            conn.execute(
                f"INSERT INTO {table}(tenant_id, {column}, granted_by_membership_id, granted_at)"
                " VALUES (?, ?, ?, ?)",
                (tenant_id, revision_id, actor_member_id, ts),
            )


def _require_published_revisions(
    conn: Any, table: str, column: str, revision_ids: Sequence[str]
) -> None:
    if not revision_ids:
        return
    placeholders = sql_in_placeholders(len(revision_ids))
    rows = conn.execute(
        f"SELECT {column} AS revision_id FROM {table}"
        f" WHERE {column} IN ({placeholders}) AND status = 'published'",
        tuple(revision_ids),
    ).fetchall()
    approved = {str(row["revision_id"]) for row in rows}
    missing = [revision_id for revision_id in revision_ids if revision_id not in approved]
    if missing:
        raise WorkBuddyCapabilityNotApproved(
            f"unknown or revoked catalog revisions: {', '.join(sorted(missing))}"
        )


def _is_active_member(conn: Any, tenant_id: str, membership_id: str) -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {_TABLE_MEMBERS}"
        " WHERE tenant_id = ? AND membership_id = ? AND status = 'active'",
        (tenant_id, membership_id),
    ).fetchone()
    return row is not None


def _require_active_member(conn: Any, tenant_id: str, membership_id: str) -> None:
    if not _is_active_member(conn, tenant_id, membership_id):
        raise WorkBuddyMembershipRequired("acting membership is not active in this tenant")


def _is_id_collection(value: object) -> TypeGuard[Iterable[str]]:
    """True when the caller supplied a revision id collection (empty set clears)."""
    return isinstance(value, (list, tuple, set, frozenset))


def _normalized_ids(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values))


def _validated_external_ref(value: str) -> str:
    ref = str(value).strip()
    if not _EXTERNAL_REF_RE.match(ref) or len(ref) > _MAX_REF_LENGTH:
        raise WorkBuddyInvalidInput(
            "external_ref must be a server-generated pointer such as vault://tenant/credential",
            code=CODE_CREDENTIAL_REF_INVALID,
        )
    return ref


def _validated_text(value: str, *, field: str, max_length: int) -> str:
    text = str(value).strip()
    if not text or len(text) > max_length:
        raise WorkBuddyInvalidInput(f"{field} must be between 1 and {max_length} characters")
    return text


def _validated_description(value: str) -> str:
    text = str(value)
    if len(text) > _MAX_DESCRIPTION_LENGTH:
        raise WorkBuddyInvalidInput(
            f"description must be at most {_MAX_DESCRIPTION_LENGTH} characters"
        )
    return text


def _json_text_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    value = json.loads(str(raw))
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)

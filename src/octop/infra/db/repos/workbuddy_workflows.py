"""WorkBuddy workflow storage: mutable drafts, immutable versions, bindings.

PostgreSQL is the fact store.  Every read and write runs through
:func:`octop.infra.db.workbuddy_context.workbuddy_transaction`, so the tenant is
never taken from a request body and RLS (``app.tenant_id``) applies to each
statement; the explicit ``tenant_id`` predicates are defence in depth for
deployments that connect as a superuser.

Invariants owned here:

* ``workbuddy_workflows.revision`` is the compare-and-swap unit (integer, one
  step per accepted mutation).  A stale ``expected_revision`` raises
  :class:`RevisionConflict` and nothing is written.
* versions are immutable: rows are only inserted, and the definition is stored
  together with the SHA-256 of its canonical form; the repository re-verifies
  the hash before it persists anything.
* ``version_number`` is allocated as ``MAX + 1`` while the workflow row is
  locked ``FOR UPDATE``, so concurrent writers cannot fork a number.
* proposal candidates (``origin = 'proposal'``) can never be activated
  directly; promotion must create a new version.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    require_postgres,
    workbuddy_transaction,
)
from octop.infra.errors import ErrorCode
from octop.infra.workbuddy.workflow_compiler import (
    ACTIVATABLE_VERSION_ORIGINS,
    VERSION_ORIGINS,
    SemanticDecision,
)
from octop.infra.workbuddy.workflow_compiler import (
    definition_sha256 as workflow_definition_sha256,
)

WORKFLOW_COLUMNS = (
    "workflow_id, tenant_id, name, description, status, revision, active_version_id, "
    "shadow_version_id, created_by, created_by_membership_id, created_at, updated_at, archived_at"
)
VERSION_COLUMNS = (
    "workflow_version_id, tenant_id, workflow_id, version_number, definition, definition_sha256, "
    "origin, base_version_id, source_version_id, change_summary, created_by, "
    "created_by_membership_id, created_at"
)
BINDING_COLUMNS = (
    "tenant_id, workflow_version_id, binding_key, tool_name, credential_revision_id, "
    "binding_sha256, created_at"
)

ACTIVATION_MODES = frozenset({"active", "shadow"})


class WorkBuddyWorkflowError(RuntimeError):
    """A workflow-store refusal with a stable code for the API boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class WorkflowNotFound(WorkBuddyWorkflowError):
    """The workflow is absent, or invisible to this tenant (never 403)."""

    def __init__(self, message: str = "workflow not found") -> None:
        super().__init__(ErrorCode.NOT_FOUND.value, message)


class WorkflowVersionNotFound(WorkBuddyWorkflowError):
    """The version is absent, or belongs to another workflow or tenant."""

    def __init__(self, message: str = "workflow version not found") -> None:
        super().__init__(ErrorCode.WORKBUDDY_WORKFLOW_VERSION_NOT_FOUND.value, message)


class RevisionConflict(WorkBuddyWorkflowError):
    """A compare-and-swap on ``workbuddy_workflows.revision`` lost."""

    def __init__(self, message: str = "workflow revision conflict") -> None:
        super().__init__(ErrorCode.WORKBUDDY_WORKFLOW_REVISION_CONFLICT.value, message)


class VersionNotActivatable(WorkBuddyWorkflowError):
    """The version cannot become the active pointer (candidate or revoked)."""

    def __init__(self, message: str = "workflow version cannot be activated") -> None:
        super().__init__(ErrorCode.WORKBUDDY_WORKFLOW_VERSION_NOT_ACTIVATABLE.value, message)


class WorkflowInvalid(WorkBuddyWorkflowError):
    """The request cannot produce a valid immutable version."""

    def __init__(self, message: str = "workflow definition is invalid") -> None:
        super().__init__(ErrorCode.WORKBUDDY_WORKFLOW_INVALID.value, message)


class DependencyUnavailable(WorkBuddyWorkflowError):
    """A required dependency (table, resolver) could not be proven."""

    def __init__(self, message: str = "workflow dependency is unavailable") -> None:
        super().__init__(ErrorCode.WORKBUDDY_DEPENDENCY_UNAVAILABLE.value, message)


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    tenant_id: str
    workflow_id: str
    name: str
    description: str | None
    status: str
    revision: int
    active_version_id: str | None
    shadow_version_id: str | None
    created_by: int | None
    created_by_membership_id: str | None
    created_at: int
    updated_at: int
    archived_at: int | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkflowRecord:
        return cls(
            tenant_id=str(row["tenant_id"]),
            workflow_id=str(row["workflow_id"]),
            name=str(row["name"]),
            description=row["description"],
            status=str(row["status"]),
            revision=int(row["revision"]),
            active_version_id=(str(row["active_version_id"]) if row["active_version_id"] else None),
            shadow_version_id=(str(row["shadow_version_id"]) if row["shadow_version_id"] else None),
            created_by=int(row["created_by"]) if row["created_by"] is not None else None,
            created_by_membership_id=(
                str(row["created_by_membership_id"]) if row["created_by_membership_id"] else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
            archived_at=int(row["archived_at"]) if row["archived_at"] is not None else None,
        )


@dataclass(frozen=True, slots=True)
class WorkflowVersionRecord:
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    version_number: int
    definition: Mapping[str, Any]
    definition_sha256: str
    origin: str
    base_version_id: str | None
    source_version_id: str | None
    change_summary: str | None
    created_by: int | None
    created_by_membership_id: str | None
    created_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkflowVersionRecord:
        raw = row["definition"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        return cls(
            tenant_id=str(row["tenant_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_version_id=str(row["workflow_version_id"]),
            version_number=int(row["version_number"]),
            definition=raw,
            definition_sha256=str(row["definition_sha256"]),
            origin=str(row["origin"]),
            base_version_id=str(row["base_version_id"]) if row["base_version_id"] else None,
            source_version_id=str(row["source_version_id"]) if row["source_version_id"] else None,
            change_summary=row["change_summary"],
            created_by=int(row["created_by"]) if row["created_by"] is not None else None,
            created_by_membership_id=(
                str(row["created_by_membership_id"]) if row["created_by_membership_id"] else None
            ),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class WorkflowVersionBundle:
    workflow: WorkflowRecord
    version: WorkflowVersionRecord


@dataclass(frozen=True, slots=True)
class ToolBindingRecord:
    tenant_id: str
    workflow_version_id: str
    binding_key: str
    tool_name: str
    credential_revision_id: str | None
    binding_sha256: str
    created_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ToolBindingRecord:
        return cls(
            tenant_id=str(row["tenant_id"]),
            workflow_version_id=str(row["workflow_version_id"]),
            binding_key=str(row["binding_key"]),
            tool_name=str(row["tool_name"]),
            credential_revision_id=(
                str(row["credential_revision_id"]) if row["credential_revision_id"] else None
            ),
            binding_sha256=str(row["binding_sha256"]),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class RevocationRecord:
    tenant_id: str
    revocation_id: str
    workflow_id: str
    workflow_version_id: str | None
    reason: str
    revoked_by: int | None
    revoked_by_membership_id: str | None
    revoked_at: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> RevocationRecord:
        return cls(
            tenant_id=str(row["tenant_id"]),
            revocation_id=str(row["revocation_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_version_id=(
                str(row["workflow_version_id"]) if row["workflow_version_id"] else None
            ),
            reason=str(row["reason"]),
            revoked_by=int(row["revoked_by"]) if row["revoked_by"] is not None else None,
            revoked_by_membership_id=(
                str(row["revoked_by_membership_id"]) if row["revoked_by_membership_id"] else None
            ),
            revoked_at=int(row["revoked_at"]),
        )


def _public_uuid(value: str, *, message: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkflowNotFound(message) from exc


def binding_sha256(tool_name: str, credential_revision_id: str | None) -> str:
    """Deterministic hash of a tool binding, pinned with its version."""
    return workflow_definition_sha256(
        {"tool_name": str(tool_name), "credential_revision_id": credential_revision_id or ""}
    )


class WorkBuddyWorkflowRepo:
    """PostgreSQL-backed workflow store; every method is tenant-scoped."""

    def __init__(self, db: DatabasePool) -> None:
        require_postgres(db)
        self._db = db

    # -- plumbing ---------------------------------------------------------- #

    @contextmanager
    def _connection(
        self,
        tenant_id: str,
        *,
        user_id: int | None = None,
        conn: Any | None = None,
    ) -> Iterator[Any]:
        """Own transaction, or the caller's WorkBuddy transaction when given."""
        if conn is not None:
            yield conn
            return
        context = WorkBuddyDbContext.for_tenant(tenant_id, user_id=user_id)
        with workbuddy_transaction(self._db, context) as opened:
            yield opened

    @staticmethod
    def _fetch_workflow(conn: Any, tenant_id: str, workflow_id: str) -> WorkflowRecord | None:
        row = conn.execute(
            f"SELECT {WORKFLOW_COLUMNS} FROM workbuddy_workflows "
            "WHERE tenant_id = ? AND workflow_id = ?",
            (tenant_id, workflow_id),
        ).fetchone()
        return WorkflowRecord.from_row(row) if row is not None else None

    @staticmethod
    def _lock_revision(conn: Any, tenant_id: str, workflow_id: str, expected_revision: int) -> int:
        row = conn.execute(
            "SELECT revision FROM workbuddy_workflows "
            "WHERE tenant_id = ? AND workflow_id = ? FOR UPDATE",
            (tenant_id, workflow_id),
        ).fetchone()
        if row is None:
            raise WorkflowNotFound()
        current = int(row["revision"])
        if int(expected_revision) != current:
            raise RevisionConflict(
                f"workflow revision {expected_revision} does not match {current}"
            )
        return current

    @staticmethod
    def _next_version_number(conn: Any, tenant_id: str, workflow_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 AS next_number "
            "FROM workbuddy_workflow_versions WHERE tenant_id = ? AND workflow_id = ?",
            (tenant_id, workflow_id),
        ).fetchone()
        return int(row["next_number"]) if row is not None else 1

    @staticmethod
    def _require_definition(definition: Any, expected_sha256: str) -> dict[str, Any]:
        """Only the compiler's canonical definition is ever persisted."""
        if not isinstance(definition, Mapping):
            raise WorkflowInvalid("definition must be a JSON object")
        actual = workflow_definition_sha256(definition)
        if actual != str(expected_sha256):
            raise WorkflowInvalid("definition does not match its canonical hash")
        return dict(definition)

    @staticmethod
    def _insert_version(
        conn: Any,
        *,
        tenant_id: str,
        workflow_id: str,
        version_number: int,
        definition: Mapping[str, Any],
        definition_sha256: str,
        origin: str,
        base_version_id: str | None,
        source_version_id: str | None,
        change_summary: str | None,
        created_by_user_id: int | None,
        created_by_membership_id: str | None,
    ) -> WorkflowVersionRecord:
        row = conn.execute(
            f"INSERT INTO workbuddy_workflow_versions (tenant_id, workflow_id, version_number, "
            f"definition, definition_sha256, origin, base_version_id, source_version_id, "
            f"change_summary, created_by, created_by_membership_id, created_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING {VERSION_COLUMNS}",
            (
                tenant_id,
                workflow_id,
                version_number,
                # The column is jsonb and psycopg cannot adapt a dict, so bind the
                # serialized form the way the other WorkBuddy repos do.
                json.dumps(dict(definition), sort_keys=True, separators=(",", ":")),
                definition_sha256,
                origin,
                base_version_id,
                source_version_id,
                change_summary,
                created_by_user_id,
                created_by_membership_id,
                now_ts(),
            ),
        ).fetchone()
        if row is None:
            raise WorkflowInvalid("workflow version insert returned no row")
        return WorkflowVersionRecord.from_row(row)

    # -- reads ------------------------------------------------------------- #

    def get_workflow(
        self, tenant_id: str, workflow_id: str, *, conn: Any | None = None
    ) -> WorkflowRecord | None:
        """One workflow, or ``None`` when it is absent or invisible."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        with self._connection(tenant_id, conn=conn) as connection:
            return self._fetch_workflow(connection, tenant_id, workflow_id)

    def list_workflows(
        self, tenant_id: str, *, user_id: int | None = None, conn: Any | None = None
    ) -> list[WorkflowRecord]:
        """Every workflow visible in this tenant, newest first."""
        with self._connection(tenant_id, user_id=user_id, conn=conn) as connection:
            rows = connection.execute(
                f"SELECT {WORKFLOW_COLUMNS} FROM workbuddy_workflows "
                "WHERE tenant_id = ? ORDER BY updated_at DESC, workflow_id",
                (tenant_id,),
            ).fetchall()
        return [WorkflowRecord.from_row(row) for row in rows]

    def get_version(
        self,
        tenant_id: str,
        workflow_id: str,
        workflow_version_id: str,
        *,
        conn: Any | None = None,
    ) -> WorkflowVersionRecord | None:
        """One immutable version; the workflow id is part of the identity."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        workflow_version_id = _public_uuid(
            workflow_version_id, message="workflow version not found"
        )
        with self._connection(tenant_id, conn=conn) as connection:
            row = connection.execute(
                f"SELECT {VERSION_COLUMNS} FROM workbuddy_workflow_versions "
                "WHERE tenant_id = ? AND workflow_id = ? AND workflow_version_id = ?",
                (tenant_id, workflow_id, workflow_version_id),
            ).fetchone()
        return WorkflowVersionRecord.from_row(row) if row is not None else None

    def list_versions(
        self, tenant_id: str, workflow_id: str, *, conn: Any | None = None
    ) -> list[WorkflowVersionRecord]:
        """Version history, newest first."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        with self._connection(tenant_id, conn=conn) as connection:
            rows = connection.execute(
                f"SELECT {VERSION_COLUMNS} FROM workbuddy_workflow_versions "
                "WHERE tenant_id = ? AND workflow_id = ? ORDER BY version_number DESC",
                (tenant_id, workflow_id),
            ).fetchall()
        return [WorkflowVersionRecord.from_row(row) for row in rows]

    def load_active_version(
        self, tenant_id: str, workflow_id: str, *, conn: Any | None = None
    ) -> WorkflowVersionRecord | None:
        """The published version a new execution must lock onto."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        with self._connection(tenant_id, conn=conn) as connection:
            row = connection.execute(
                f"SELECT {', '.join(f'v.{column}' for column in VERSION_COLUMNS.split(', '))} "
                "FROM workbuddy_workflows w JOIN workbuddy_workflow_versions v "
                "ON v.tenant_id = w.tenant_id AND v.workflow_id = w.workflow_id "
                "AND v.workflow_version_id = w.active_version_id "
                "WHERE w.tenant_id = ? AND w.workflow_id = ?",
                (tenant_id, workflow_id),
            ).fetchone()
        return WorkflowVersionRecord.from_row(row) if row is not None else None

    def list_tool_bindings(
        self, tenant_id: str, workflow_version_id: str, *, conn: Any | None = None
    ) -> list[ToolBindingRecord]:
        with self._connection(tenant_id, conn=conn) as connection:
            rows = connection.execute(
                f"SELECT {BINDING_COLUMNS} FROM workbuddy_workflow_version_tool_bindings "
                "WHERE tenant_id = ? AND workflow_version_id = ? ORDER BY binding_key",
                (tenant_id, workflow_version_id),
            ).fetchall()
        return [ToolBindingRecord.from_row(row) for row in rows]

    def list_revocations(
        self, tenant_id: str, workflow_id: str, *, conn: Any | None = None
    ) -> list[RevocationRecord]:
        with self._connection(tenant_id, conn=conn) as connection:
            rows = connection.execute(
                "SELECT tenant_id, revocation_id, workflow_id, workflow_version_id, reason, "
                "revoked_by, revoked_by_membership_id, revoked_at "
                "FROM workbuddy_workflow_revocations "
                "WHERE tenant_id = ? AND workflow_id = ? ORDER BY revoked_at DESC",
                (tenant_id, workflow_id),
            ).fetchall()
        return [RevocationRecord.from_row(row) for row in rows]

    def is_version_revoked(
        self, tenant_id: str, workflow_id: str, workflow_version_id: str, *, conn: Any | None = None
    ) -> bool:
        """A version is revoked when it or its whole workflow was withdrawn."""
        with self._connection(tenant_id, conn=conn) as connection:
            row = connection.execute(
                "SELECT 1 AS revoked FROM workbuddy_workflow_revocations "
                "WHERE tenant_id = ? AND workflow_id = ? "
                "AND (workflow_version_id IS NULL OR workflow_version_id = ?) LIMIT 1",
                (tenant_id, workflow_id, workflow_version_id),
            ).fetchone()
        return row is not None

    # -- writes ------------------------------------------------------------ #

    def create_workflow(
        self,
        tenant_id: str,
        *,
        name: str,
        description: str | None = None,
        definition: Mapping[str, Any],
        definition_sha256: str,
        created_by_user_id: int | None,
        created_by_membership_id: str | None,
        change_summary: str | None = None,
        origin: str = "save",
        conn: Any | None = None,
    ) -> WorkflowVersionBundle:
        """Create a draft plus its first immutable version."""
        name = str(name).strip()
        if not 1 <= len(name) <= 200:
            raise WorkflowInvalid("workflow name must be 1-200 characters")
        if description is not None and len(str(description)) > 2000:
            raise WorkflowInvalid("workflow description is too long")
        if origin not in VERSION_ORIGINS:
            raise WorkflowInvalid(f"unsupported version origin {origin!r}")
        stored = self._require_definition(definition, definition_sha256)
        created_at = now_ts()
        with self._connection(tenant_id, user_id=created_by_user_id, conn=conn) as connection:
            row = connection.execute(
                f"INSERT INTO workbuddy_workflows (tenant_id, name, description, status, revision, "
                f"created_by, created_by_membership_id, created_at, updated_at) "
                f"VALUES (?, ?, ?, 'draft', 1, ?, ?, ?, ?) RETURNING {WORKFLOW_COLUMNS}",
                (
                    tenant_id,
                    name,
                    description,
                    created_by_user_id,
                    created_by_membership_id,
                    created_at,
                    created_at,
                ),
            ).fetchone()
            if row is None:
                raise WorkflowInvalid("workflow insert returned no row")
            workflow = WorkflowRecord.from_row(row)
            version = self._insert_version(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow.workflow_id,
                version_number=1,
                definition=stored,
                definition_sha256=str(definition_sha256),
                origin=origin,
                base_version_id=None,
                source_version_id=None,
                change_summary=change_summary,
                created_by_user_id=created_by_user_id,
                created_by_membership_id=created_by_membership_id,
            )
        return WorkflowVersionBundle(workflow=workflow, version=version)

    def save_version(
        self,
        tenant_id: str,
        workflow_id: str,
        *,
        definition: Mapping[str, Any],
        definition_sha256: str,
        expected_revision: int,
        created_by_user_id: int | None,
        created_by_membership_id: str | None,
        base_version_id: str | None = None,
        origin: str = "save",
        source_version_id: str | None = None,
        change_summary: str | None = None,
        conn: Any | None = None,
    ) -> WorkflowVersionBundle:
        """Append one immutable version and advance the revision (CAS)."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        if origin not in VERSION_ORIGINS:
            raise WorkflowInvalid(f"unsupported version origin {origin!r}")
        stored = self._require_definition(definition, definition_sha256)
        with self._connection(tenant_id, user_id=created_by_user_id, conn=conn) as connection:
            self._lock_revision(connection, tenant_id, workflow_id, expected_revision)
            version = self._insert_version(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                version_number=self._next_version_number(connection, tenant_id, workflow_id),
                definition=stored,
                definition_sha256=str(definition_sha256),
                origin=origin,
                base_version_id=base_version_id,
                source_version_id=source_version_id,
                change_summary=change_summary,
                created_by_user_id=created_by_user_id,
                created_by_membership_id=created_by_membership_id,
            )
            row = connection.execute(
                f"UPDATE workbuddy_workflows SET revision = revision + 1, updated_at = ? "
                f"WHERE tenant_id = ? AND workflow_id = ? AND revision = ? "
                f"RETURNING {WORKFLOW_COLUMNS}",
                (now_ts(), tenant_id, workflow_id, int(expected_revision)),
            ).fetchone()
            if row is None:
                raise RevisionConflict()
            workflow = WorkflowRecord.from_row(row)
        return WorkflowVersionBundle(workflow=workflow, version=version)

    def activate_version(
        self,
        tenant_id: str,
        workflow_id: str,
        workflow_version_id: str,
        *,
        expected_revision: int,
        mode: str = "active",
        conn: Any | None = None,
    ) -> WorkflowRecord:
        """Point ``active_version_id`` (or ``shadow_version_id``) at a version."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        workflow_version_id = _public_uuid(
            workflow_version_id, message="workflow version not found"
        )
        if mode not in ACTIVATION_MODES:
            raise WorkflowInvalid(f"unsupported activation mode {mode!r}")
        with self._connection(tenant_id, conn=conn) as connection:
            self._lock_revision(connection, tenant_id, workflow_id, expected_revision)
            version = connection.execute(
                f"SELECT {VERSION_COLUMNS} FROM workbuddy_workflow_versions "
                "WHERE tenant_id = ? AND workflow_id = ? AND workflow_version_id = ?",
                (tenant_id, workflow_id, workflow_version_id),
            ).fetchone()
            if version is None:
                raise WorkflowVersionNotFound()
            origin = str(version["origin"])
            if origin not in ACTIVATABLE_VERSION_ORIGINS:
                raise VersionNotActivatable(
                    f"version with origin {origin!r} is a candidate and cannot be activated"
                )
            revoked = connection.execute(
                "SELECT 1 AS revoked FROM workbuddy_workflow_revocations "
                "WHERE tenant_id = ? AND workflow_id = ? "
                "AND (workflow_version_id IS NULL OR workflow_version_id = ?) LIMIT 1",
                (tenant_id, workflow_id, workflow_version_id),
            ).fetchone()
            if revoked is not None:
                raise VersionNotActivatable("version is revoked")
            if mode == "shadow":
                statement = (
                    "UPDATE workbuddy_workflows SET shadow_version_id = ?, revision = revision + 1, "
                    f"updated_at = ? WHERE tenant_id = ? AND workflow_id = ? AND revision = ? "
                    f"RETURNING {WORKFLOW_COLUMNS}"
                )
                params: Sequence[Any] = (
                    workflow_version_id,
                    now_ts(),
                    tenant_id,
                    workflow_id,
                    int(expected_revision),
                )
            else:
                statement = (
                    "UPDATE workbuddy_workflows SET active_version_id = ?, status = 'active', "
                    "revision = revision + 1, updated_at = ? "
                    f"WHERE tenant_id = ? AND workflow_id = ? AND revision = ? "
                    f"RETURNING {WORKFLOW_COLUMNS}"
                )
                params = (
                    workflow_version_id,
                    now_ts(),
                    tenant_id,
                    workflow_id,
                    int(expected_revision),
                )
            row = connection.execute(statement, params).fetchone()
            if row is None:
                raise RevisionConflict()
        return WorkflowRecord.from_row(row)

    def rollback_version(
        self,
        tenant_id: str,
        workflow_id: str,
        source_version_id: str,
        *,
        expected_revision: int,
        created_by_user_id: int | None,
        created_by_membership_id: str | None,
        change_summary: str | None = None,
        conn: Any | None = None,
    ) -> WorkflowVersionBundle:
        """Copy a historical snapshot into a new version and publish it."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        source_version_id = _public_uuid(source_version_id, message="workflow version not found")
        with self._connection(tenant_id, user_id=created_by_user_id, conn=conn) as connection:
            self._lock_revision(connection, tenant_id, workflow_id, expected_revision)
            source = connection.execute(
                f"SELECT {VERSION_COLUMNS} FROM workbuddy_workflow_versions "
                "WHERE tenant_id = ? AND workflow_id = ? AND workflow_version_id = ?",
                (tenant_id, workflow_id, source_version_id),
            ).fetchone()
            if source is None:
                raise WorkflowVersionNotFound()
            revoked = connection.execute(
                "SELECT 1 AS revoked FROM workbuddy_workflow_revocations "
                "WHERE tenant_id = ? AND workflow_id = ? "
                "AND (workflow_version_id IS NULL OR workflow_version_id = ?) LIMIT 1",
                (tenant_id, workflow_id, source_version_id),
            ).fetchone()
            if revoked is not None:
                raise VersionNotActivatable("source version is revoked")
            snapshot = WorkflowVersionRecord.from_row(source)
            current = self._fetch_workflow(connection, tenant_id, workflow_id)
            version = self._insert_version(
                connection,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                version_number=self._next_version_number(connection, tenant_id, workflow_id),
                definition=snapshot.definition,
                definition_sha256=snapshot.definition_sha256,
                origin="rollback",
                base_version_id=current.active_version_id if current is not None else None,
                source_version_id=source_version_id,
                change_summary=change_summary or f"rollback to version {snapshot.version_number}",
                created_by_user_id=created_by_user_id,
                created_by_membership_id=created_by_membership_id,
            )
            row = connection.execute(
                "UPDATE workbuddy_workflows SET active_version_id = ?, status = 'active', "
                "revision = revision + 1, updated_at = ? "
                f"WHERE tenant_id = ? AND workflow_id = ? AND revision = ? "
                f"RETURNING {WORKFLOW_COLUMNS}",
                (
                    version.workflow_version_id,
                    now_ts(),
                    tenant_id,
                    workflow_id,
                    int(expected_revision),
                ),
            ).fetchone()
            if row is None:
                raise RevisionConflict()
        return WorkflowVersionBundle(workflow=WorkflowRecord.from_row(row), version=version)

    def bind_tool(
        self,
        tenant_id: str,
        workflow_version_id: str,
        *,
        binding_key: str,
        tool_name: str,
        credential_revision_id: str | None = None,
        conn: Any | None = None,
    ) -> ToolBindingRecord:
        """Pin one tool (and credential revision) to an immutable version."""
        binding_key = str(binding_key).strip()
        tool_name = str(tool_name).strip()
        if not binding_key or not tool_name:
            raise WorkflowInvalid("tool binding needs a key and a tool name")
        with self._connection(tenant_id, conn=conn) as connection:
            row = connection.execute(
                f"INSERT INTO workbuddy_workflow_version_tool_bindings ({BINDING_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING " + BINDING_COLUMNS,
                (
                    tenant_id,
                    workflow_version_id,
                    binding_key,
                    tool_name,
                    credential_revision_id,
                    binding_sha256(tool_name, credential_revision_id),
                    now_ts(),
                ),
            ).fetchone()
            if row is None:
                raise WorkflowInvalid("tool binding insert returned no row")
        return ToolBindingRecord.from_row(row)

    def revoke_version(
        self,
        tenant_id: str,
        workflow_id: str,
        workflow_version_id: str | None,
        *,
        reason: str,
        revoked_by_user_id: int | None,
        revoked_by_membership_id: str | None,
        conn: Any | None = None,
    ) -> RevocationRecord:
        """Record an append-only revocation for one version or a whole workflow."""
        workflow_id = _public_uuid(workflow_id, message="workflow not found")
        if workflow_version_id is not None:
            workflow_version_id = _public_uuid(
                workflow_version_id, message="workflow version not found"
            )
        reason = str(reason).strip()
        if not reason:
            raise WorkflowInvalid("revocation needs a reason")
        with self._connection(tenant_id, conn=conn) as connection:
            row = connection.execute(
                "INSERT INTO workbuddy_workflow_revocations (tenant_id, workflow_id, "
                "workflow_version_id, reason, revoked_by, revoked_by_membership_id, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "RETURNING tenant_id, revocation_id, workflow_id, workflow_version_id, reason, "
                "revoked_by, revoked_by_membership_id, revoked_at",
                (
                    tenant_id,
                    workflow_id,
                    workflow_version_id,
                    reason,
                    revoked_by_user_id,
                    revoked_by_membership_id,
                    now_ts(),
                ),
            ).fetchone()
            if row is None:
                raise WorkflowInvalid("revocation insert returned no row")
        return RevocationRecord.from_row(row)


class PostgresWorkflowSemanticResolver:
    """Resolve tool/model/knowledge-base/approver references inside a tenant.

    Runs on the caller's WorkBuddy transaction connection, so RLS and the
    explicit tenant predicate both apply.  A dependency that cannot be queried
    raises :class:`DependencyUnavailable` instead of guessing, and a reference
    that is not provable is refused rather than silently accepted.
    """

    def __init__(self, conn: Any, tenant_id: str, *, user_id: int | None = None) -> None:
        self._conn = conn
        self._tenant_id = tenant_id
        self._user_id = user_id

    def _one(self, sql: str, params: Sequence[Any]) -> Any | None:
        try:
            return self._conn.execute(sql, tuple(params)).fetchone()
        except Exception as exc:  # noqa: BLE001 - any failure must fail closed
            raise DependencyUnavailable(
                "workflow semantic resolver cannot query the tenant catalog"
            ) from exc

    def check_tool(self, tool_name: str, parameters: Mapping[str, Any]) -> SemanticDecision:
        row = self._one(
            "SELECT 1 AS granted FROM workbuddy_tenant_tool_grants g "
            "JOIN workbuddy_platform_tool_revisions r ON r.tool_revision_id = g.tool_revision_id "
            "WHERE g.tenant_id = ? AND r.tool_key = ? AND r.status = 'published' LIMIT 1",
            (self._tenant_id, str(tool_name)),
        )
        if row is None:
            return SemanticDecision.refused(
                "WORKFLOW_TOOL_UNAVAILABLE",
                f"tool {tool_name!r} is not granted to this tenant",
            )
        return SemanticDecision.allowed()

    def check_model(self, model: str | None) -> SemanticDecision:
        if model is None:
            row = self._one(
                "SELECT r.model_key FROM workbuddy_tenant_capabilities c "
                "JOIN workbuddy_platform_model_revisions r "
                "ON r.model_revision_id = c.default_model_revision_id "
                "WHERE c.tenant_id = ? AND r.status = 'published' LIMIT 1",
                (self._tenant_id,),
            )
            if row is None:
                return SemanticDecision.refused(
                    ErrorCode.WORKBUDDY_MODEL_NOT_CONFIGURED.value,
                    "no default model is configured for this tenant",
                )
            return SemanticDecision.allowed()
        row = self._one(
            "SELECT 1 AS granted FROM workbuddy_tenant_model_grants g "
            "JOIN workbuddy_platform_model_revisions r ON r.model_revision_id = g.model_revision_id "
            "WHERE g.tenant_id = ? AND r.model_key = ? AND r.status = 'published' LIMIT 1",
            (self._tenant_id, str(model)),
        )
        if row is None:
            return SemanticDecision.refused(
                ErrorCode.WORKBUDDY_MODEL_NOT_CONFIGURED.value,
                f"model {model!r} is not approved for this tenant",
            )
        return SemanticDecision.allowed()

    def check_knowledge_base(self, knowledge_base_id: str) -> SemanticDecision:
        if self._user_id is None:
            raise DependencyUnavailable("knowledge base resolution needs the calling user context")
        row = self._one(
            "WITH RECURSIVE dept_chain(department_id) AS ("
            "SELECT m.department_id FROM workbuddy_tenant_members m "
            "WHERE m.tenant_id = ? AND m.user_id = ? AND m.department_id IS NOT NULL "
            "UNION "
            "SELECT d.parent_department_id FROM workbuddy_departments d "
            "JOIN dept_chain c ON d.department_id = c.department_id "
            "WHERE d.tenant_id = ? AND d.parent_department_id IS NOT NULL"
            ") SELECT 1 AS visible FROM workbuddy_knowledge_bases kb "
            "WHERE kb.tenant_id = ? AND kb.kb_id = ? AND kb.archived_at IS NULL AND ("
            "kb.scope = 'enterprise' OR kb.owner_user_id = ? "
            "OR (kb.scope = 'department' AND kb.department_id IN (SELECT department_id FROM dept_chain)) "
            "OR EXISTS (SELECT 1 FROM workbuddy_knowledge_acl acl WHERE acl.tenant_id = kb.tenant_id "
            "AND acl.kb_id = kb.kb_id AND acl.user_id = ?)) LIMIT 1",
            (
                self._tenant_id,
                self._user_id,
                self._tenant_id,
                self._tenant_id,
                str(knowledge_base_id),
                self._user_id,
                self._user_id,
            ),
        )
        if row is None:
            return SemanticDecision.refused(
                "WORKFLOW_KNOWLEDGE_BASE_UNKNOWN",
                "knowledge base is not visible to this member",
            )
        return SemanticDecision.allowed()

    def check_approver(self, user_id: str) -> SemanticDecision:
        row = self._one(
            "SELECT 1 AS member FROM workbuddy_tenant_members "
            "WHERE tenant_id = ? AND membership_id = ? AND status = 'active' LIMIT 1",
            (self._tenant_id, str(user_id)),
        )
        if row is None:
            return SemanticDecision.refused(
                "WORKFLOW_APPROVER_INVALID",
                "approver is not an active member of this tenant",
            )
        return SemanticDecision.allowed()


__all__ = [
    "ACTIVATION_MODES",
    "DependencyUnavailable",
    "PostgresWorkflowSemanticResolver",
    "RevisionConflict",
    "RevocationRecord",
    "ToolBindingRecord",
    "VersionNotActivatable",
    "WorkBuddyWorkflowError",
    "WorkBuddyWorkflowRepo",
    "WorkflowInvalid",
    "WorkflowNotFound",
    "WorkflowRecord",
    "WorkflowVersionBundle",
    "WorkflowVersionNotFound",
    "WorkflowVersionRecord",
    "binding_sha256",
]

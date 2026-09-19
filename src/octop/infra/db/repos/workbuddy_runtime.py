"""WorkBuddy runtime fact store (PostgreSQL only).

Every statement runs inside the shared audited transaction context so tenant
isolation comes from the database (RLS + FORCE RLS bound to ``app.tenant_id``)
and never from request bodies, headers, or queue payloads.

Rows are immutable where the contract requires it: payloads, edge runs, quota
usage and audit records are insert-only in this repo *and* have no UPDATE /
DELETE policy in ``018_workbuddy_runtime.pg.sql``. Step runs, approvals,
reconciliations, jobs and notifications are updated through explicit
compare-and-set statements that also carry the execution fence, so a stale
runner can never commit over a newer owner.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    WorkBuddyPostgresRequiredError,
    require_postgres,
    workbuddy_transaction,
)

JsonMap = Mapping[str, Any]


def new_runtime_id() -> str:
    """UUIDv4 string for a runtime row (application-generated, never from input)."""
    return str(uuid.uuid4())


def canonical_json(value: Any) -> str:
    """Deterministic JSON text used for content hashes (sorted keys, tight)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _jsonb(value: Any) -> Any:
    """Bind a value destined for a jsonb column.

    psycopg cannot adapt a dict or list, so mappings and sequences are sent as
    JSON text; strings and NULL pass through untouched. Without this every write
    to a jsonb column fails with "cannot adapt type 'dict'".
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _row_value(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _json_map(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def execution_lease_name(execution_id: str) -> str:
    """Lease name of one execution's attempt: one lease per execution.

    Parallel nodes of the same execution share it, so the fence it carries is
    also the token every write of that attempt is checked against.
    """
    return f"execution:{execution_id}"


# The oldest execution a worker may try to admit: waiting ones, plus the ones a
# crashed worker left running on a lease that has since expired. Locking only the
# execution row lets two workers scan at once and still claim one execution once.
_CLAIMABLE_EXECUTIONS_SQL = """
SELECT e.id, e.tenant_id, e.started_at
FROM workbuddy_executions e
LEFT JOIN workbuddy_leases l
  ON l.tenant_id = e.tenant_id
 AND l.lease_name = 'execution:' || e.id::text
 AND l.released_at IS NULL
 AND l.expires_at > now()
WHERE e.status IN ('queued', 'running') AND l.tenant_id IS NULL
ORDER BY e.created_at, e.id
LIMIT ?
FOR UPDATE OF e SKIP LOCKED
"""


@contextmanager
def runtime_transaction(
    db: DatabasePool,
    ctx: WorkBuddyDbContext,
    conn: Any | None = None,
) -> Iterator[Any]:
    """Yield ``conn`` unchanged, or open the shared WorkBuddy transaction.

    Passing an existing connection keeps multi-statement business operations in
    one audited transaction; omitting it opens one for a single repository call.
    """
    if conn is not None:
        yield conn
        return
    with workbuddy_transaction(db, ctx) as opened:
        yield opened


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """One execution a worker admitted and now owns under ``fence``.

    ``fence`` is the lease's monotonic fencing token: every later write of this
    attempt carries it, so a worker that lost the lease cannot commit.
    """

    tenant_id: str
    execution_id: str
    fence: int
    worker_id: str


@dataclass(frozen=True, slots=True)
class ExecutionRow:
    id: str
    tenant_id: str
    workflow_id: str
    workflow_version_id: str
    workflow_version_hash: str
    definition_snapshot: dict[str, Any]
    status: str
    trigger_type: str
    idempotency_scope: str | None
    idempotency_key: str | None
    idempotency_hash: str | None
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    error_code: str | None
    error_message: str | None
    fence: int
    created_by_user_id: int | None
    created_at: Any
    started_at: Any
    finished_at: Any
    cancel_requested_at: Any
    active_duration_ms: int
    token_usage: int
    proposal_id: str | None
    cohort: str | None
    bucket: int | None
    route_canary_percent: int | None
    subject: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExecutionRow:
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_version_id=str(row["workflow_version_id"]),
            workflow_version_hash=str(row["workflow_version_hash"]),
            definition_snapshot=_json_map(row["definition_snapshot"]),
            status=str(row["status"]),
            trigger_type=str(row["trigger_type"]),
            idempotency_scope=row["idempotency_scope"],
            idempotency_key=row["idempotency_key"],
            idempotency_hash=row["idempotency_hash"],
            inputs=_json_map(row["inputs"]),
            outputs=_json_map(row["outputs"]),
            error_code=row["error_code"],
            error_message=row["error_message"],
            fence=int(row["fence"]),
            created_by_user_id=(
                int(row["created_by_user_id"]) if row["created_by_user_id"] is not None else None
            ),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cancel_requested_at=row["cancel_requested_at"],
            active_duration_ms=int(row["active_duration_ms"] or 0),
            token_usage=int(row["token_usage"] or 0),
            proposal_id=(str(row["proposal_id"]) if row["proposal_id"] else None),
            cohort=row["cohort"],
            bucket=row["bucket"],
            route_canary_percent=row["route_canary_percent"],
            subject=row["subject"],
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in {"success", "failed", "partial", "canceled"}


@dataclass(frozen=True, slots=True)
class PayloadRow:
    id: str
    execution_id: str
    kind: str
    node_id: str | None
    sha256: str
    size_bytes: int
    content: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PayloadRow:
        return cls(
            id=str(row["id"]),
            execution_id=str(row["execution_id"]),
            kind=str(row["kind"]),
            node_id=row["node_id"],
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            content=row["content"],
        )


@dataclass(frozen=True, slots=True)
class StepRunRow:
    id: str
    execution_id: str
    node_id: str
    node_type: str
    skip_reason: str | None
    attempt: int
    status: str
    save_as: str | None
    output: Any
    error_code: str | None
    error_message: str | None
    fence: int
    duration_ms: int | None
    started_at: Any
    finished_at: Any
    tool_id: str | None = None
    tool_call_key: str | None = None
    dispatch_intent_at: Any = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> StepRunRow:
        return cls(
            id=str(row["id"]),
            execution_id=str(row["execution_id"]),
            node_id=str(row["node_id"]),
            node_type=str(row["node_type"]),
            skip_reason=row["skip_reason"],
            attempt=int(row["attempt"]),
            status=str(row["status"]),
            save_as=row["save_as"],
            output=row["output"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            fence=int(row["fence"]),
            duration_ms=row["duration_ms"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            tool_id=row["tool_id"],
            tool_call_key=row["tool_call_key"],
            dispatch_intent_at=row["dispatch_intent_at"],
        )


@dataclass(frozen=True, slots=True)
class EdgeRunRow:
    id: str
    execution_id: str
    edge_from: str
    edge_to: str
    branch: str | None
    taken: bool

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> EdgeRunRow:
        return cls(
            id=str(row["id"]),
            execution_id=str(row["execution_id"]),
            edge_from=str(row["edge_from"]),
            edge_to=str(row["edge_to"]),
            branch=row["branch"],
            taken=bool(row["taken"]),
        )


@dataclass(frozen=True, slots=True)
class ApprovalRequestRow:
    id: str
    execution_id: str
    node_id: str
    status: str
    required_approvals: int
    decided_approvals: int
    params: Any
    params_sha256: str
    token_expires_at: Any
    token_consumed_at: Any
    locked_workflow_version_id: str
    locked_workflow_version_hash: str
    decision: str | None
    decided_by_user_id: int | None
    decided_at: Any
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ApprovalRequestRow:
        return cls(
            id=str(row["id"]),
            execution_id=str(row["execution_id"]),
            node_id=str(row["node_id"]),
            status=str(row["status"]),
            required_approvals=int(row["required_approvals"]),
            decided_approvals=int(row["decided_approvals"]),
            params=row["params"],
            params_sha256=str(row["params_sha256"]),
            token_expires_at=row["token_expires_at"],
            token_consumed_at=row["token_consumed_at"],
            locked_workflow_version_id=str(row["locked_workflow_version_id"]),
            locked_workflow_version_hash=str(row["locked_workflow_version_hash"]),
            decision=row["decision"],
            decided_by_user_id=(
                int(row["decided_by_user_id"]) if row["decided_by_user_id"] is not None else None
            ),
            decided_at=row["decided_at"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class ApprovalCandidateRow:
    id: str
    approval_request_id: str
    user_id: int
    department_id: str | None
    status: str
    decided_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ApprovalCandidateRow:
        return cls(
            id=str(row["id"]),
            approval_request_id=str(row["approval_request_id"]),
            user_id=int(row["user_id"]),
            department_id=str(row["department_id"]) if row["department_id"] else None,
            status=str(row["status"]),
            decided_at=row["decided_at"],
        )


@dataclass(frozen=True, slots=True)
class ReconciliationRow:
    id: str
    execution_id: str
    step_run_id: str
    decision: str
    evidence_ref: str
    evidence_hash: str
    external_request_id: str | None
    result_payload_ref: str | None
    note: str
    decided_by_user_id: int
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ReconciliationRow:
        return cls(
            id=str(row["id"]),
            execution_id=str(row["execution_id"]),
            step_run_id=str(row["step_run_id"]),
            decision=str(row["decision"]),
            evidence_ref=str(row["evidence_ref"]),
            evidence_hash=str(row["evidence_hash"]),
            external_request_id=row["external_request_id"],
            result_payload_ref=(
                str(row["result_payload_ref"]) if row["result_payload_ref"] else None
            ),
            note=str(row["note"]),
            decided_by_user_id=int(row["decided_by_user_id"]),
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class JobRow:
    id: str
    kind: str
    status: str
    progress: int
    execution_id: str | None
    requested_by_user_id: int | None
    request_hash: str | None
    result: Any
    error_code: str | None
    error_message: str | None
    created_at: Any
    started_at: Any
    finished_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JobRow:
        return cls(
            id=str(row["id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            progress=int(row["progress"]),
            execution_id=str(row["execution_id"]) if row["execution_id"] else None,
            requested_by_user_id=(
                int(row["requested_by_user_id"])
                if row["requested_by_user_id"] is not None
                else None
            ),
            request_hash=row["request_hash"],
            result=row["result"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: str
    topic: str
    dedupe_key: str
    payload: Any
    status: str
    attempts: int

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> OutboxRow:
        return cls(
            id=str(row["id"]),
            topic=str(row["topic"]),
            dedupe_key=str(row["dedupe_key"]),
            payload=row["payload"],
            status=str(row["status"]),
            attempts=int(row["attempts"]),
        )


@dataclass(frozen=True, slots=True)
class LeaseRow:
    tenant_id: str
    lease_name: str
    holder: str
    fence: int
    expires_at: Any
    released_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LeaseRow:
        return cls(
            tenant_id=str(row["tenant_id"]),
            lease_name=str(row["lease_name"]),
            holder=str(row["holder"]),
            fence=int(row["fence"]),
            expires_at=row["expires_at"],
            released_at=row["released_at"],
        )


@dataclass(frozen=True, slots=True)
class QuotaReservationRow:
    id: str
    quota_key: str
    amount: int
    status: str
    scope: str
    execution_id: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> QuotaReservationRow:
        return cls(
            id=str(row["id"]),
            quota_key=str(row["quota_key"]),
            amount=int(row["amount"]),
            status=str(row["status"]),
            scope=str(row["scope"]),
            execution_id=str(row["execution_id"]) if row["execution_id"] else None,
        )


@dataclass(frozen=True, slots=True)
class AuditLogRow:
    id: str
    actor_user_id: int | None
    actor_kind: str
    action: str
    resource_type: str
    resource_id: str | None
    outcome: str
    details: Any
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> AuditLogRow:
        return cls(
            id=str(row["id"]),
            actor_user_id=(int(row["actor_user_id"]) if row["actor_user_id"] is not None else None),
            actor_kind=str(row["actor_kind"]),
            action=str(row["action"]),
            resource_type=str(row["resource_type"]),
            resource_id=row["resource_id"],
            outcome=str(row["outcome"]),
            details=row["details"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class NotificationRow:
    id: str
    user_id: int
    kind: str
    title: str
    body: str | None
    resource_type: str | None
    resource_id: str | None
    read_at: Any
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> NotificationRow:
        return cls(
            id=str(row["id"]),
            user_id=int(row["user_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            body=row["body"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            read_at=row["read_at"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class ChatSessionRow:
    id: str
    user_id: int
    title: str
    message_count: int
    created_at: Any
    updated_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ChatSessionRow:
        return cls(
            id=str(row["id"]),
            user_id=int(row["user_id"]),
            title=str(row["title"]),
            message_count=int(row["message_count"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True, slots=True)
class ChatMessageRow:
    id: str
    session_id: str
    role: str
    content: str
    model_revision: str | None
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ChatMessageRow:
        return cls(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            role=str(row["role"]),
            content=str(row["content"]),
            model_revision=row["model_revision"],
            created_at=row["created_at"],
        )


class WorkBuddyRuntimeRepo:
    """Fact-store access for executions, approvals, jobs, quota, audit, chat."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def tenant_status(self, tenant_id: str, *, conn: Any | None = None) -> str | None:
        """Lifecycle status of a tenant from the identity fact store.

        Read under the platform context on purpose: the runtime must be able to
        refuse a suspended tenant's start even when no request principal exists
        (cron, webhook, or event trigger).
        """
        ctx = WorkBuddyDbContext.platform()
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT status FROM workbuddy_tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return None if row is None else str(row["status"])

    # -- executions ---------------------------------------------------------

    def insert_execution(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        workflow_id: str,
        workflow_version_id: str,
        workflow_version_hash: str,
        definition_snapshot: JsonMap,
        trigger_type: str,
        inputs: JsonMap,
        created_by_user_id: int | None,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        idempotency_hash: str | None = None,
        execution_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = execution_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_executions(
                    id, tenant_id, workflow_id, workflow_version_id, workflow_version_hash,
                    definition_snapshot, status, trigger_type, idempotency_scope,
                    idempotency_key, idempotency_hash, inputs, created_by_user_id,
                    proposal_id, cohort, bucket, route_canary_percent, subject
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    workflow_id,
                    workflow_version_id,
                    workflow_version_hash,
                    _jsonb(definition_snapshot),
                    trigger_type,
                    idempotency_scope,
                    idempotency_key,
                    idempotency_hash,
                    _jsonb(inputs),
                    created_by_user_id,
                ),
            )
        return rid

    def get_execution(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> ExecutionRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_executions WHERE id = ?", (execution_id,)
            ).fetchone()
        return ExecutionRow.from_row(row) if row is not None else None

    def get_execution_by_idempotency(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        scope: str,
        key: str,
        conn: Any | None = None,
    ) -> ExecutionRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                """
                SELECT * FROM workbuddy_executions
                WHERE tenant_id = ? AND idempotency_scope = ? AND idempotency_key = ?
                """,
                (tenant_id, scope, key),
            ).fetchone()
        return ExecutionRow.from_row(row) if row is not None else None

    def insert_execution_if_absent(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        workflow_id: str,
        workflow_version_id: str,
        workflow_version_hash: str,
        definition_snapshot: JsonMap,
        trigger_type: str,
        inputs: JsonMap,
        created_by_user_id: int | None,
        execution_id: str,
        idempotency_scope: str | None = None,
        idempotency_key: str | None = None,
        idempotency_hash: str | None = None,
        proposal_id: str | None = None,
        cohort: str | None = None,
        bucket: int | None = None,
        route_canary_percent: int | None = None,
        subject: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        """Insert a new execution; False when the idempotency key already exists.

        The partial unique index decides the winner, so two concurrent requests
        with the same key can never both create an execution (the loser reads
        the winner's row and compares request hashes instead of failing).
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                INSERT INTO workbuddy_executions(
                    id, tenant_id, workflow_id, workflow_version_id, workflow_version_hash,
                    definition_snapshot, status, trigger_type, idempotency_scope,
                    idempotency_key, idempotency_hash, inputs, created_by_user_id,
                    proposal_id, cohort, bucket, route_canary_percent, subject
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, idempotency_scope, idempotency_key)
                  WHERE idempotency_key IS NOT NULL DO NOTHING
                """,
                (
                    execution_id,
                    tenant_id,
                    workflow_id,
                    workflow_version_id,
                    workflow_version_hash,
                    _jsonb(definition_snapshot),
                    trigger_type,
                    idempotency_scope,
                    idempotency_key,
                    idempotency_hash,
                    _jsonb(inputs),
                    created_by_user_id,
                    proposal_id,
                    cohort,
                    bucket,
                    route_canary_percent,
                    subject,
                ),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def execution_counts(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        created_by_user_id: int | None = None,
        since: Any | None = None,
        conn: Any | None = None,
    ) -> dict[str, int]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if created_by_user_id is not None:
            clauses.append("created_by_user_id = ?")
            params.append(int(created_by_user_id))
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT status, COUNT(*) AS total FROM workbuddy_executions "
                f"WHERE {' AND '.join(clauses)} GROUP BY status",
                tuple(params),
            ).fetchall()
        return {str(row["status"]): int(row["total"]) for row in rows}

    def list_executions(
        self,
        ctx: WorkBuddyDbContext,
        *,
        created_by_user_id: int | None = None,
        workflow_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[ExecutionRow]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if created_by_user_id is not None:
            clauses.append("created_by_user_id = ?")
            params.append(created_by_user_id)
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM workbuddy_executions WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [ExecutionRow.from_row(r) for r in rows]

    def update_execution_status(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        status: str,
        expected_status: Sequence[str],
        expected_fence: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        outputs: JsonMap | None = None,
        active_duration_ms: int | None = None,
        token_usage: int | None = None,
        mark_started: bool = False,
        mark_finished: bool = False,
        conn: Any | None = None,
    ) -> bool:
        """Compare-and-set the execution state; False means the CAS did not apply."""
        placeholders = ", ".join("?" for _ in expected_status)
        assignments = ["status = ?"]
        params: list[Any] = [status]
        if error_code is not None:
            assignments.append("error_code = ?")
            params.append(error_code)
        if error_message is not None:
            assignments.append("error_message = ?")
            params.append(error_message)
        if outputs is not None:
            assignments.append("outputs = ?")
            params.append(_jsonb(outputs))
        if active_duration_ms is not None:
            # Attempts add up: every attempt's measured step time is active time.
            assignments.append("active_duration_ms = active_duration_ms + ?")
            params.append(int(active_duration_ms))
        if token_usage is not None:
            assignments.append("token_usage = token_usage + ?")
            params.append(int(token_usage))
        if mark_started:
            assignments.append("started_at = COALESCE(started_at, now())")
        if mark_finished:
            assignments.append("finished_at = now()")
        params.append(execution_id)
        params.extend(expected_status)
        sql = (
            f"UPDATE workbuddy_executions SET {', '.join(assignments)} "
            f"WHERE id = ? AND status IN ({placeholders})"
        )
        if expected_fence is not None:
            # The attempt's fencing token: a worker that lost its lease cannot
            # commit this write, even if it reached the statement late.
            sql += " AND fence = ?"
            params.append(int(expected_fence))
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(sql, tuple(params))
            return bool(getattr(cursor, "rowcount", 0))

    def request_cancel(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> bool:
        """Cancel an execution that is not running; report False when it is.

        A queued or parked execution has no call in flight, so it can be canceled
        outright. A *running* one may be in the middle of an external call, and
        the contract forbids claiming to undo a write that may already have
        happened: the request is recorded instead (``request_cancel_running``)
        and the run converges to ``canceled`` once its current call lands.
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_executions
                SET status = 'canceled', cancel_requested_at = now(), finished_at = now()
                WHERE id = ? AND status IN ('queued', 'waiting_approval')
                """,
                (execution_id,),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def request_cancel_running(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> bool:
        """Ask a running execution to stop before its next step.

        The runner notices between nodes and settles the attempt as ``canceled``;
        until then the execution keeps the status and the slot it holds, which is
        what makes the cancel honest about the call already in flight.
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_executions
                SET cancel_requested_at = COALESCE(cancel_requested_at, now())
                WHERE id = ? AND status = 'running'
                """,
                (execution_id,),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def cancel_requested(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> bool:
        """True once a cancel has been requested for this execution."""
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT cancel_requested_at FROM workbuddy_executions WHERE id = ?",
                (execution_id,),
            ).fetchone()
        return row is not None and row["cancel_requested_at"] is not None

    def cancel_pending_steps(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        attempt: int,
        fence: int,
        conn: Any | None = None,
    ) -> int:
        """Cancel the steps this attempt queued but never decided."""
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_step_runs
                SET status = 'canceled', finished_at = now()
                WHERE execution_id = ? AND attempt = ? AND fence = ?
                  AND status IN ('queued', 'running')
                """,
                (execution_id, int(attempt), int(fence)),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

    def latest_succeeded_execution(
        self, ctx: WorkBuddyDbContext, workflow_id: str, *, conn: Any | None = None
    ) -> str | None:
        """The newest successful run of a workflow: the shadow replay source."""
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT id FROM workbuddy_executions"
                " WHERE workflow_id = ? AND status = 'success'"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
        return str(row["id"]) if row is not None else None

    def recorded_outputs(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> dict[str, Any]:
        """What each settled step of an execution produced, by node id."""
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT node_id, output FROM workbuddy_step_runs"
                " WHERE execution_id = ? AND status = 'success' AND output IS NOT NULL"
                " ORDER BY attempt, node_id",
                (execution_id,),
            ).fetchall()
        return {str(row["node_id"]): row["output"] for row in rows}

    def canary_metrics(
        self,
        ctx: WorkBuddyDbContext,
        proposal_id: str,
        *,
        window_start: int | None = None,
        window_end: int | None = None,
        conn: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Settled executions of one evaluation, with the time each one spent.

        Active time is what the steps measured; the rest of the execution's life
        was waiting for a human or an operator, and the contract wants the two
        reported apart.
        """
        clauses = [
            "proposal_id = ?",
            "cohort IN ('canary', 'baseline')",
            # Only settled executions count as samples; a parked one is still
            # waiting and has no outcome to compare.
            "status IN ('success', 'failed', 'partial', 'canceled')",
        ]
        params: list[Any] = [proposal_id]
        if window_start is not None:
            clauses.append("created_at >= to_timestamp(?)")
            params.append(float(window_start))
        if window_end is not None:
            # The window is measured in whole seconds, so it covers the end second
            # completely: a sample created 84ms into it is inside, not outside.
            clauses.append("created_at < to_timestamp(?)")
            params.append(float(window_end) + 1.0)
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT cohort, status, active_duration_ms, token_usage,"
                " GREATEST(EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000"
                "          - active_duration_ms, 0) AS wait_ms"
                f" FROM workbuddy_executions WHERE {' AND '.join(clauses)}"
                " ORDER BY created_at, id",
                tuple(params),
            ).fetchall()
        return [
            {
                "cohort": str(row["cohort"]),
                "status": str(row["status"]),
                "active_duration_ms": int(row["active_duration_ms"] or 0),
                "token_usage": int(row["token_usage"] or 0),
                "wait_ms": int(float(row["wait_ms"] or 0)),
            }
            for row in rows
        ]

    def request_cancel_deferred(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> bool:
        """Record the cancel request on an execution parked for reconciliation.

        The contract keeps the execution in ``waiting_reconciliation`` until the
        unknown write is reconciled; only then does it converge to ``canceled``.
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_executions
                SET cancel_requested_at = now()
                WHERE id = ? AND status = 'waiting_reconciliation'
                  AND cancel_requested_at IS NULL
                """,
                (execution_id,),
            )
            return bool(getattr(cursor, "rowcount", 0))

    # -- payloads and step/edge facts --------------------------------------

    def insert_payload(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        kind: str,
        content: Any,
        sha256: str,
        size_bytes: int,
        node_id: str | None = None,
        payload_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = payload_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_execution_payloads(
                    id, tenant_id, execution_id, kind, node_id, sha256, size_bytes, content
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, tenant_id, execution_id, kind, node_id, sha256, size_bytes, _jsonb(content)),
            )
        return rid

    def list_payloads(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        conn: Any | None = None,
    ) -> list[PayloadRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_execution_payloads WHERE execution_id = ? "
                "ORDER BY created_at, id",
                (execution_id,),
            ).fetchall()
        return [PayloadRow.from_row(r) for r in rows]

    def insert_step_run(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        node_id: str,
        node_type: str,
        status: str,
        fence: int,
        attempt: int = 1,
        save_as: str | None = None,
        input_sha256: str | None = None,
        output_sha256: str | None = None,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
        skip_reason: str | None = None,
        started_at: float | None = None,
        step_run_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = step_run_id or new_runtime_id()
        finished_at = "now()" if status in {"success", "failed", "skipped"} else "NULL"
        # ``started_at`` is the wall clock the engine measured before the node ran,
        # so a step's window is its own and not the moment the run was persisted.
        started = "to_timestamp(?)" if started_at is not None else "now()"
        started_param: tuple[Any, ...] = (float(started_at),) if started_at is not None else ()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                f"""
                INSERT INTO workbuddy_step_runs(
                    id, tenant_id, execution_id, node_id, node_type, attempt, status, save_as,
                    input_sha256, output_sha256, output, error_code, error_message,
                    fence, duration_ms, skip_reason, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {started}, {finished_at})
                """,
                (
                    rid,
                    tenant_id,
                    execution_id,
                    node_id,
                    node_type,
                    attempt,
                    status,
                    save_as,
                    input_sha256,
                    output_sha256,
                    _jsonb(output),
                    error_code,
                    error_message,
                    fence,
                    duration_ms,
                    skip_reason,
                    *started_param,
                ),
            )
        return rid

    def queue_step_runs(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        nodes: Sequence[tuple[str, str]],
        attempt: int,
        fence: int,
        conn: Any | None = None,
    ) -> int:
        """Record the attempt's pending steps: one ``queued`` row per node.

        A node an earlier attempt already settled keeps that row -- it is a
        replay or a permanent failure -- so the newest attempt can never mask a
        fact the engine still has to honour. What is left is exactly the work
        this attempt has not decided yet, which is what ``queued`` means.
        """
        inserted = 0
        with runtime_transaction(self._db, ctx, conn) as c:
            for node_id, node_type in nodes:
                cursor = c.execute(
                    """
                    INSERT INTO workbuddy_step_runs(
                        id, tenant_id, execution_id, node_id, node_type, attempt, status, fence
                    )
                    SELECT ?, ?, ?, ?, ?, ?, 'queued', ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM workbuddy_step_runs settled
                        WHERE settled.tenant_id = ? AND settled.execution_id = ?
                          AND settled.node_id = ?
                          AND settled.status IN ('success', 'failed', 'skipped', 'canceled')
                    )
                    ON CONFLICT (tenant_id, execution_id, node_id, attempt) DO NOTHING
                    """,
                    (
                        new_runtime_id(),
                        tenant_id,
                        execution_id,
                        node_id,
                        node_type,
                        int(attempt),
                        int(fence),
                        tenant_id,
                        execution_id,
                        node_id,
                    ),
                )
                inserted += int(getattr(cursor, "rowcount", 0) or 0)
        return inserted

    def mark_step_running(
        self,
        ctx: WorkBuddyDbContext,
        *,
        execution_id: str,
        node_id: str,
        attempt: int,
        fence: int,
        conn: Any | None = None,
    ) -> bool:
        """Move one pending step to ``running``; only its own fence may."""
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_step_runs
                SET status = 'running'
                WHERE execution_id = ? AND node_id = ? AND attempt = ? AND fence = ?
                  AND status = 'queued'
                """,
                (execution_id, node_id, int(attempt), int(fence)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def settle_attempt_step(
        self,
        ctx: WorkBuddyDbContext,
        *,
        execution_id: str,
        node_id: str,
        attempt: int,
        fence: int,
        status: str,
        save_as: str | None = None,
        output_sha256: str | None = None,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        duration_ms: int | None = None,
        skip_reason: str | None = None,
        started_at: float | None = None,
        conn: Any | None = None,
    ) -> bool:
        """Settle the row this attempt queued, in place.

        False means the step was not pending under this fence: either the attempt
        is not the one that queued it, or another worker owns the execution now.
        """
        finished_at = "now()" if status in {"success", "failed", "skipped"} else "NULL"
        started = ", started_at = to_timestamp(?)" if started_at is not None else ""
        params: list[Any] = [
            status,
            save_as,
            output_sha256,
            _jsonb(output),
            error_code,
            error_message,
            duration_ms,
            skip_reason,
        ]
        if started_at is not None:
            params.append(float(started_at))
        params.extend([execution_id, node_id, int(attempt), int(fence)])
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                f"""
                UPDATE workbuddy_step_runs
                SET status = ?, save_as = ?, output_sha256 = ?, output = ?, error_code = ?,
                    error_message = ?, duration_ms = ?, skip_reason = ?, finished_at = {finished_at}
                    {started}
                WHERE execution_id = ? AND node_id = ? AND attempt = ? AND fence = ?
                  AND status IN ('queued', 'running')
                """,
                tuple(params),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def mark_step_dispatch_intent(
        self,
        ctx: WorkBuddyDbContext,
        *,
        execution_id: str,
        node_id: str,
        attempt: int,
        fence: int,
        tool_id: str | None,
        tool_call_key: str,
        conn: Any | None = None,
    ) -> bool:
        """Record what this attempt is about to call, before it calls it.

        Written once per attempt: the first decision to dispatch is the intent,
        and a second write would only move the moment the call left the process.
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_step_runs
                SET tool_id = ?, tool_call_key = ?, dispatch_intent_at = now()
                WHERE execution_id = ? AND node_id = ? AND attempt = ? AND fence = ?
                  AND status = 'running' AND dispatch_intent_at IS NULL
                """,
                (tool_id, tool_call_key, execution_id, node_id, int(attempt), int(fence)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def list_step_runs(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> list[StepRunRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_step_runs WHERE execution_id = ? "
                "ORDER BY started_at, attempt, node_id",
                (execution_id,),
            ).fetchall()
        return [StepRunRow.from_row(r) for r in rows]

    def insert_edge_run(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        edge_from: str,
        edge_to: str,
        taken: bool,
        branch: str | None = None,
        edge_run_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = edge_run_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_edge_runs(
                    id, tenant_id, execution_id, edge_from, edge_to, branch, taken
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id, execution_id, edge_from, edge_to) DO NOTHING
                """,
                (rid, tenant_id, execution_id, edge_from, edge_to, branch, bool(taken)),
            )
        return rid

    def list_edge_runs(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> list[EdgeRunRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_edge_runs WHERE execution_id = ? ORDER BY created_at, id",
                (execution_id,),
            ).fetchall()
        return [EdgeRunRow.from_row(r) for r in rows]

    # -- approvals ----------------------------------------------------------

    def insert_approval_request(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        node_id: str,
        required_approvals: int,
        params: Any,
        params_sha256: str,
        locked_workflow_version_id: str,
        locked_workflow_version_hash: str,
        approval_request_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = approval_request_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_approval_requests(
                    id, tenant_id, execution_id, node_id, status, required_approvals,
                    params_sha256, params, locked_workflow_version_id,
                    locked_workflow_version_hash
                ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    execution_id,
                    node_id,
                    required_approvals,
                    params_sha256,
                    _jsonb(params),
                    locked_workflow_version_id,
                    locked_workflow_version_hash,
                ),
            )
        return rid

    def insert_approval_candidates(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        approval_request_id: str,
        candidates: Sequence[tuple[int, str | None]],
        conn: Any | None = None,
    ) -> int:
        inserted = 0
        with runtime_transaction(self._db, ctx, conn) as c:
            for user_id, department_id in candidates:
                c.execute(
                    """
                    INSERT INTO workbuddy_approval_candidates(
                        id, tenant_id, approval_request_id, user_id, department_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        new_runtime_id(),
                        tenant_id,
                        approval_request_id,
                        int(user_id),
                        department_id,
                    ),
                )
                inserted += 1
        return inserted

    def get_approval_request(
        self, ctx: WorkBuddyDbContext, approval_request_id: str, *, conn: Any | None = None
    ) -> ApprovalRequestRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_approval_requests WHERE id = ?", (approval_request_id,)
            ).fetchone()
        return ApprovalRequestRow.from_row(row) if row is not None else None

    def list_approval_requests(
        self,
        ctx: WorkBuddyDbContext,
        *,
        execution_id: str | None = None,
        candidate_user_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[ApprovalRequestRow]:
        clauses = ["r.tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if execution_id is not None:
            clauses.append("r.execution_id = ?")
            params.append(execution_id)
        if status is not None:
            clauses.append("r.status = ?")
            params.append(status)
        if candidate_user_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM workbuddy_approval_candidates cand "
                "WHERE cand.approval_request_id = r.id AND cand.user_id = ?)"
            )
            params.append(int(candidate_user_id))
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT r.* FROM workbuddy_approval_requests r "
                f"WHERE {' AND '.join(clauses)} ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [ApprovalRequestRow.from_row(r) for r in rows]

    def list_approval_candidates(
        self, ctx: WorkBuddyDbContext, approval_request_id: str, *, conn: Any | None = None
    ) -> list[ApprovalCandidateRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_approval_candidates WHERE approval_request_id = ? "
                "ORDER BY created_at, user_id",
                (approval_request_id,),
            ).fetchall()
        return [ApprovalCandidateRow.from_row(r) for r in rows]

    def issue_approval_challenge(
        self,
        ctx: WorkBuddyDbContext,
        approval_request_id: str,
        *,
        token_hash: str,
        expires_at: Any,
        conn: Any | None = None,
    ) -> bool:
        """Replace any previous one-time challenge token (only the hash is stored)."""
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_approval_requests
                SET token_hash = ?, token_expires_at = ?, token_consumed_at = NULL
                WHERE id = ? AND status = 'pending'
                """,
                (token_hash, expires_at, approval_request_id),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def consume_approval_token(
        self,
        ctx: WorkBuddyDbContext,
        approval_request_id: str,
        *,
        token_hash: str,
        conn: Any | None = None,
    ) -> bool:
        """One-shot consume; expiry, single use and state are checked in one CAS.

        ``token_expires_at > now()`` and ``token_consumed_at IS NULL`` are part of
        the WHERE clause, so a token that expires between the read and the write
        can never be consumed twice (expiration race safety).
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_approval_requests
                SET token_consumed_at = now()
                WHERE id = ? AND status = 'pending' AND token_hash = ?
                  AND token_consumed_at IS NULL AND token_expires_at > now()
                """,
                (approval_request_id, token_hash),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def decide_candidate(
        self,
        ctx: WorkBuddyDbContext,
        approval_request_id: str,
        *,
        user_id: int,
        decision: str,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_approval_candidates
                SET status = ?, decided_at = now()
                WHERE approval_request_id = ? AND user_id = ? AND status = 'pending'
                """,
                (decision, approval_request_id, int(user_id)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def count_decided_approvals(
        self, ctx: WorkBuddyDbContext, approval_request_id: str, *, conn: Any | None = None
    ) -> int:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS decided FROM workbuddy_approval_candidates
                WHERE approval_request_id = ? AND status = 'approved'
                """,
                (approval_request_id,),
            ).fetchone()
        return int(_row_value(row, "decided", 0) or 0)

    def settle_approval_request(
        self,
        ctx: WorkBuddyDbContext,
        approval_request_id: str,
        *,
        status: str,
        decision: str,
        decided_by_user_id: int | None,
        decided_approvals: int,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_approval_requests
                SET status = ?, decision = ?, decided_by_user_id = ?,
                    decided_approvals = ?, decided_at = now()
                WHERE id = ? AND status = 'pending'
                """,
                (status, decision, decided_by_user_id, int(decided_approvals), approval_request_id),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def invalidate_pending_approvals(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        conn: Any | None = None,
    ) -> int:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_approval_requests
                SET status = 'invalidated', decided_at = now()
                WHERE execution_id = ? AND status = 'pending'
                """,
                (execution_id,),
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

    # -- reconciliations ----------------------------------------------------

    def insert_reconciliation(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        step_run_id: str,
        decision: str,
        evidence_ref: str,
        evidence_hash: str,
        decided_by_user_id: int,
        note: str,
        external_request_id: str | None = None,
        result_payload_ref: str | None = None,
        reconciliation_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = reconciliation_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_reconciliations(
                    id, tenant_id, execution_id, step_run_id, decision, evidence_ref,
                    evidence_hash, external_request_id, result_payload_ref, note,
                    decided_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    execution_id,
                    step_run_id,
                    decision,
                    evidence_ref,
                    evidence_hash,
                    external_request_id,
                    result_payload_ref,
                    note,
                    int(decided_by_user_id),
                ),
            )
        return rid

    def get_payload(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        payload_id: str,
        *,
        conn: Any | None = None,
    ) -> PayloadRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_execution_payloads WHERE execution_id = ? AND id = ?",
                (execution_id, payload_id),
            ).fetchone()
        return PayloadRow.from_row(row) if row is not None else None

    def find_step_run(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        node_id: str,
        *,
        status: str | None = None,
        conn: Any | None = None,
    ) -> StepRunRow | None:
        clause = "" if status is None else " AND status = ?"
        params: tuple[Any, ...] = (
            (execution_id, node_id) if status is None else (execution_id, node_id, status)
        )
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_step_runs WHERE execution_id = ? AND node_id = ?"
                f"{clause} ORDER BY attempt DESC, started_at DESC LIMIT 1",
                params,
            ).fetchone()
        return StepRunRow.from_row(row) if row is not None else None

    def settle_step_run(
        self,
        ctx: WorkBuddyDbContext,
        step_run_id: str,
        *,
        status: str,
        output: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        """Backfill the parked attempt: only a waiting step can be settled."""
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_step_runs
                SET status = ?, output = ?, error_code = ?, error_message = ?, finished_at = now()
                WHERE id = ? AND status = 'waiting_reconciliation'
                """,
                (
                    status,
                    _jsonb(output) if output is not None else None,
                    error_code,
                    error_message,
                    step_run_id,
                ),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def list_reconciliations(
        self, ctx: WorkBuddyDbContext, execution_id: str, *, conn: Any | None = None
    ) -> list[ReconciliationRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_reconciliations WHERE execution_id = ? "
                "ORDER BY created_at, id",
                (execution_id,),
            ).fetchall()
        return [ReconciliationRow.from_row(r) for r in rows]

    # -- jobs ---------------------------------------------------------------

    def insert_job(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        kind: str,
        requested_by_user_id: int | None,
        execution_id: str | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        status: str = "queued",
        progress: int = 0,
        job_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = job_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_jobs(
                    id, tenant_id, kind, status, progress, execution_id,
                    requested_by_user_id, idempotency_key, request_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    kind,
                    status,
                    int(progress),
                    execution_id,
                    requested_by_user_id,
                    idempotency_key,
                    request_hash,
                ),
            )
        return rid

    def get_job(
        self, ctx: WorkBuddyDbContext, job_id: str, *, conn: Any | None = None
    ) -> JobRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute("SELECT * FROM workbuddy_jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRow.from_row(row) if row is not None else None

    def get_job_by_idempotency(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        kind: str,
        idempotency_key: str,
        conn: Any | None = None,
    ) -> JobRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                """
                SELECT * FROM workbuddy_jobs
                WHERE tenant_id = ? AND kind = ? AND idempotency_key = ?
                """,
                (tenant_id, kind, idempotency_key),
            ).fetchone()
        return JobRow.from_row(row) if row is not None else None

    def list_jobs(
        self,
        ctx: WorkBuddyDbContext,
        *,
        requested_by_user_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[JobRow]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if requested_by_user_id is not None:
            clauses.append("requested_by_user_id = ?")
            params.append(int(requested_by_user_id))
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM workbuddy_jobs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [JobRow.from_row(r) for r in rows]

    def finish_job(
        self,
        ctx: WorkBuddyDbContext,
        job_id: str,
        *,
        status: str,
        progress: int,
        result: Any = None,
        error_code: str | None = None,
        error_message: str | None = None,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_jobs
                SET status = ?, progress = ?, result = ?, error_code = ?, error_message = ?,
                    finished_at = now()
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (
                    status,
                    int(progress),
                    # ``result`` is jsonb: psycopg cannot adapt a mapping, so the
                    # caller's value is sent as JSON text like every other jsonb
                    # write in this module.
                    _jsonb(result) if result is not None else None,
                    error_code,
                    error_message,
                    job_id,
                ),
            )
            return bool(getattr(cursor, "rowcount", 0))

    # -- outbox -------------------------------------------------------------

    def enqueue_outbox(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        topic: str,
        dedupe_key: str,
        payload: JsonMap,
        outbox_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = outbox_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_outbox(id, tenant_id, topic, dedupe_key, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (rid, tenant_id, topic, dedupe_key, _jsonb(payload)),
            )
        return rid

    def list_pending_outbox(
        self, ctx: WorkBuddyDbContext, *, limit: int = 50, conn: Any | None = None
    ) -> list[OutboxRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_outbox WHERE status = 'pending' "
                "ORDER BY available_at, created_at LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [OutboxRow.from_row(r) for r in rows]

    def mark_outbox_dispatched(
        self, ctx: WorkBuddyDbContext, outbox_id: str, *, conn: Any | None = None
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_outbox
                SET status = 'dispatched', dispatched_at = now(), attempts = attempts + 1
                WHERE id = ? AND status = 'pending'
                """,
                (outbox_id,),
            )
            return bool(getattr(cursor, "rowcount", 0))

    # -- leases and fences --------------------------------------------------

    def acquire_lease(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        lease_name: str,
        holder: str,
        ttl_seconds: int,
        conn: Any | None = None,
    ) -> int | None:
        """Take (or take over) a lease and return its new fence, or None if held.

        Takeover bumps the fence monotonically; every later write from the
        previous holder carries the old fence and is rejected by compare-and-set.
        """
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_leases WHERE tenant_id = ? AND lease_name = ? FOR UPDATE",
                (tenant_id, lease_name),
            ).fetchone()
            if row is None:
                c.execute(
                    """
                    INSERT INTO workbuddy_leases(
                        tenant_id, lease_name, holder, fence, expires_at, released_at
                    ) VALUES (?, ?, ?, 1, now() + make_interval(secs => ?), NULL)
                    """,
                    (tenant_id, lease_name, holder, int(ttl_seconds)),
                )
                return 1
            cursor = c.execute(
                """
                UPDATE workbuddy_leases
                SET holder = ?, fence = fence + 1, acquired_at = now(),
                    expires_at = now() + make_interval(secs => ?), released_at = NULL
                WHERE tenant_id = ? AND lease_name = ?
                  AND (released_at IS NOT NULL OR expires_at <= now() OR holder = ?)
                """,
                (holder, int(ttl_seconds), tenant_id, lease_name, holder),
            )
            if not getattr(cursor, "rowcount", 0):
                return None
            updated = c.execute(
                "SELECT fence FROM workbuddy_leases WHERE tenant_id = ? AND lease_name = ?",
                (tenant_id, lease_name),
            ).fetchone()
            return int(_row_value(updated, "fence", 0) or 0)

    def verify_fence(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        lease_name: str,
        holder: str,
        fence: int,
        conn: Any | None = None,
    ) -> bool:
        """Heartbeat that fails once another holder has taken the lease."""
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_leases
                SET expires_at = now() + make_interval(secs => 60)
                WHERE tenant_id = ? AND lease_name = ? AND holder = ? AND fence = ?
                  AND released_at IS NULL
                """,
                (tenant_id, lease_name, holder, int(fence)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def release_lease(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        lease_name: str,
        holder: str,
        fence: int,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_leases
                SET released_at = now()
                WHERE tenant_id = ? AND lease_name = ? AND holder = ? AND fence = ?
                """,
                (tenant_id, lease_name, holder, int(fence)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    # -- worker claims ------------------------------------------------------

    def claim_execution(
        self,
        *,
        worker_id: str,
        lease_ttl_seconds: int,
        reservation_ttl_seconds: int,
        scan_limit: int = 50,
    ) -> ExecutionClaim | None:
        """Claim the oldest admissible execution for ``worker_id``.

        Everything the contract asks of admission happens in this one
        transaction: the tenant's row is locked so two workers can never count
        the same free slot, a suspended tenant starts nothing new (work already
        in flight still finishes), the slot a crashed worker left behind is
        reused instead of doubled, the lease is taken with a monotonic fence, and
        the execution moves to ``running`` under that fence.

        ``running`` executions whose lease expired are claimable, which is how a
        crashed worker's attempt is taken over. Returns None when nothing can be
        admitted right now.
        """
        ctx = WorkBuddyDbContext.platform()
        with runtime_transaction(self._db, ctx) as c:
            candidates = c.execute(_CLAIMABLE_EXECUTIONS_SQL, (max(1, int(scan_limit)),)).fetchall()
            for row in candidates:
                execution_id = str(row["id"])
                tenant_id = str(row["tenant_id"])
                started = row["started_at"] is not None
                tenant = c.execute(
                    "SELECT status FROM workbuddy_tenants WHERE tenant_id = ? FOR UPDATE",
                    (tenant_id,),
                ).fetchone()
                if tenant is None:
                    continue
                if str(_row_value(tenant, "status", "")) != "active" and not started:
                    continue
                if not self._holds_slot(c, tenant_id=tenant_id, execution_id=execution_id):
                    limit = self._concurrency_limit(c, tenant_id)
                    if limit is not None and self._live_slot_count(c, tenant_id) + 1 > limit:
                        continue
                    self._reserve_slot(
                        c,
                        tenant_id=tenant_id,
                        execution_id=execution_id,
                        ttl_seconds=reservation_ttl_seconds,
                    )
                lease_name = execution_lease_name(execution_id)
                fence = self.acquire_lease(
                    ctx,
                    tenant_id=tenant_id,
                    lease_name=lease_name,
                    holder=worker_id,
                    ttl_seconds=lease_ttl_seconds,
                    conn=c,
                )
                if fence is None:
                    continue
                cursor = c.execute(
                    """
                    UPDATE workbuddy_executions
                    SET status = 'running', started_at = COALESCE(started_at, now()), fence = ?
                    WHERE id = ? AND status IN ('queued', 'running')
                    """,
                    (int(fence), execution_id),
                )
                if not getattr(cursor, "rowcount", 0):
                    # Cancelled between the scan and the claim: hand the lease back.
                    self.release_lease(
                        ctx,
                        tenant_id=tenant_id,
                        lease_name=lease_name,
                        holder=worker_id,
                        fence=int(fence),
                        conn=c,
                    )
                    continue
                return ExecutionClaim(
                    tenant_id=tenant_id,
                    execution_id=execution_id,
                    fence=int(fence),
                    worker_id=worker_id,
                )
        return None

    def requeue_execution(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        expected_status: Sequence[str],
        conn: Any | None = None,
    ) -> bool:
        """Send a decided execution back to the admission queue.

        A parked execution released its running slot when it parked, so resuming
        it re-applies for one through the worker instead of running inside the
        approver's request.
        """
        placeholders = ", ".join("?" for _ in expected_status)
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                f"UPDATE workbuddy_executions SET status = 'queued' "
                f"WHERE id = ? AND status IN ({placeholders})",
                (execution_id, *expected_status),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def _holds_slot(self, c: Any, *, tenant_id: str, execution_id: str) -> bool:
        row = c.execute(
            "SELECT id FROM workbuddy_quota_reservations "
            "WHERE tenant_id = ? AND quota_key = 'concurrency' AND execution_id = ? "
            "AND status = 'reserved' AND expires_at > now() LIMIT 1",
            (tenant_id, execution_id),
        ).fetchone()
        return row is not None

    def _concurrency_limit(self, c: Any, tenant_id: str) -> int | None:
        row = c.execute(
            """
            SELECT coalesce(q.limit_value, m.default_limit) AS limit_value
            FROM workbuddy_quota_metrics m
            LEFT JOIN workbuddy_tenant_quotas q
              ON q.metric = m.metric AND q.tenant_id = ?
            WHERE m.metric = 'concurrency'
            """,
            (tenant_id,),
        ).fetchone()
        if row is None:
            return None
        value = _row_value(row, "limit_value", None)
        return int(value) if value is not None else None

    def _live_slot_count(self, c: Any, tenant_id: str) -> int:
        row = c.execute(
            "SELECT coalesce(sum(amount), 0) AS total FROM workbuddy_quota_reservations "
            "WHERE tenant_id = ? AND quota_key = 'concurrency' AND status = 'reserved' "
            "AND expires_at > now()",
            (tenant_id,),
        ).fetchone()
        return int(_row_value(row, "total", 0) or 0)

    def _reserve_slot(self, c: Any, *, tenant_id: str, execution_id: str, ttl_seconds: int) -> None:
        c.execute(
            """
            INSERT INTO workbuddy_quota_reservations(
                id, tenant_id, quota_key, amount, status, scope, execution_id, expires_at
            ) VALUES (?, ?, 'concurrency', 1, 'reserved', 'execution', ?,
                      now() + make_interval(secs => ?))
            """,
            (new_runtime_id(), tenant_id, execution_id, int(ttl_seconds)),
        )

    # -- quota --------------------------------------------------------------

    def lock_tenant_quota(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        conn: Any,
    ) -> None:
        """Serialize quota checks and reservations for one tenant transaction."""
        row = conn.execute(
            "SELECT tenant_id FROM workbuddy_tenants WHERE tenant_id = ? FOR UPDATE",
            (tenant_id,),
        ).fetchone()
        if row is None:
            raise ValueError("tenant does not exist")

    def reserve_quota(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        quota_key: str,
        amount: int,
        scope: str,
        ttl_seconds: int = 3600,
        execution_id: str | None = None,
        reservation_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = reservation_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_quota_reservations(
                    id, tenant_id, quota_key, amount, status, scope, execution_id, expires_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, now() + make_interval(secs => ?))
                """,
                (rid, tenant_id, quota_key, int(amount), scope, execution_id, int(ttl_seconds)),
            )
        return rid

    def settle_quota_reservation(
        self,
        ctx: WorkBuddyDbContext,
        reservation_id: str,
        *,
        status: str,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_quota_reservations
                SET status = ?, settled_at = now()
                WHERE id = ? AND status = 'reserved'
                """,
                (status, reservation_id),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def list_live_quota_reservations(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        quota_key: str | None = None,
        execution_id: str | None = None,
        conn: Any | None = None,
    ) -> list[QuotaReservationRow]:
        clauses = ["tenant_id = ?", "status = 'reserved'", "expires_at > now()"]
        params: list[Any] = [tenant_id]
        if quota_key is not None:
            clauses.append("quota_key = ?")
            params.append(quota_key)
        if execution_id is not None:
            clauses.append("execution_id = ?")
            params.append(execution_id)
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM workbuddy_quota_reservations WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchall()
        return [QuotaReservationRow.from_row(row) for row in rows]

    def record_quota_usage(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        quota_key: str,
        amount: int,
        direction: str,
        scope: str,
        execution_id: str | None = None,
        job_id: str | None = None,
        reservation_id: str | None = None,
        usage_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = usage_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_quota_usage(
                    id, tenant_id, quota_key, amount, direction, scope,
                    execution_id, job_id, reservation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    quota_key,
                    int(amount),
                    direction,
                    scope,
                    execution_id,
                    job_id,
                    reservation_id,
                ),
            )
        return rid

    def quota_usage_total(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        quota_key: str | None = None,
        since: Any | None = None,
        conn: Any | None = None,
    ) -> int:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if quota_key is not None:
            clauses.append("quota_key = ?")
            params.append(quota_key)
        if since is not None:
            clauses.append("recorded_at >= ?")
            params.append(since)
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                f"SELECT COALESCE(SUM(amount), 0) AS total FROM workbuddy_quota_usage "
                f"WHERE {' AND '.join(clauses)}",
                tuple(params),
            ).fetchone()
        return int(_row_value(row, "total", 0) or 0)

    # -- audit, notifications, chat ----------------------------------------

    def append_audit(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        actor_user_id: int | None,
        actor_kind: str,
        action: str,
        resource_type: str,
        outcome: str,
        resource_id: str | None = None,
        details: JsonMap | None = None,
        audit_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        """Append-only audit record; callers must pass secret-free details."""
        rid = audit_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_audit_logs(
                    id, tenant_id, actor_user_id, actor_kind, action, resource_type,
                    resource_id, outcome, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    actor_user_id,
                    actor_kind,
                    action,
                    resource_type,
                    resource_id,
                    outcome,
                    _jsonb(details or {}),
                ),
            )
        return rid

    def list_audit_logs(
        self,
        ctx: WorkBuddyDbContext,
        *,
        action: str | None = None,
        resource_type: str | None = None,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[AuditLogRow]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [ctx.tenant_id]
        if action is not None:
            clauses.append("action = ?")
            params.append(action)
        if resource_type is not None:
            clauses.append("resource_type = ?")
            params.append(resource_type)
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM workbuddy_audit_logs WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [AuditLogRow.from_row(r) for r in rows]

    def insert_notification(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        user_id: int,
        kind: str,
        title: str,
        body: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        notification_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = notification_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_notifications(
                    id, tenant_id, user_id, kind, title, body, resource_type, resource_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, tenant_id, int(user_id), kind, title, body, resource_type, resource_id),
            )
        return rid

    def list_notifications(
        self,
        ctx: WorkBuddyDbContext,
        *,
        user_id: int,
        unread_only: bool = False,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[NotificationRow]:
        clauses = ["tenant_id = ?", "user_id = ?"]
        params: list[Any] = [ctx.tenant_id, int(user_id)]
        if unread_only:
            clauses.append("read_at IS NULL")
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                f"SELECT * FROM workbuddy_notifications WHERE {' AND '.join(clauses)} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [NotificationRow.from_row(r) for r in rows]

    def mark_notification_read(
        self,
        ctx: WorkBuddyDbContext,
        notification_id: str,
        *,
        user_id: int,
        conn: Any | None = None,
    ) -> bool:
        with runtime_transaction(self._db, ctx, conn) as c:
            cursor = c.execute(
                """
                UPDATE workbuddy_notifications
                SET read_at = COALESCE(read_at, now())
                WHERE id = ? AND user_id = ?
                """,
                (notification_id, int(user_id)),
            )
            return bool(getattr(cursor, "rowcount", 0))

    def create_chat_session(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        user_id: int,
        title: str = "",
        session_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = session_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_chat_sessions(id, tenant_id, user_id, title)
                VALUES (?, ?, ?, ?)
                """,
                (rid, tenant_id, int(user_id), title[:200]),
            )
        return rid

    def get_chat_session(
        self, ctx: WorkBuddyDbContext, session_id: str, *, conn: Any | None = None
    ) -> ChatSessionRow | None:
        with runtime_transaction(self._db, ctx, conn) as c:
            row = c.execute(
                "SELECT * FROM workbuddy_chat_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return ChatSessionRow.from_row(row) if row is not None else None

    def list_chat_sessions(
        self,
        ctx: WorkBuddyDbContext,
        *,
        user_id: int,
        limit: int = 50,
        conn: Any | None = None,
    ) -> list[ChatSessionRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                """
                SELECT * FROM workbuddy_chat_sessions
                WHERE tenant_id = ? AND user_id = ?
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (ctx.tenant_id, int(user_id), max(1, min(int(limit), 200))),
            ).fetchall()
        return [ChatSessionRow.from_row(r) for r in rows]

    def insert_chat_message(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
        model_revision: str | None = None,
        usage: JsonMap | None = None,
        message_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        rid = message_id or new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_chat_messages(
                    id, tenant_id, session_id, role, content, model_revision, usage
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, tenant_id, session_id, role, _jsonb(content), model_revision, _jsonb(usage)),
            )
            c.execute(
                """
                UPDATE workbuddy_chat_sessions
                SET message_count = message_count + 1, updated_at = now()
                WHERE id = ?
                """,
                (session_id,),
            )
        return rid

    def list_chat_messages(
        self,
        ctx: WorkBuddyDbContext,
        session_id: str,
        *,
        limit: int = 100,
        conn: Any | None = None,
    ) -> list[ChatMessageRow]:
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_chat_messages WHERE session_id = ? "
                "ORDER BY created_at, id LIMIT ?",
                (session_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [ChatMessageRow.from_row(r) for r in rows]

    # -- maintenance --------------------------------------------------------

    def purge_tenant(
        self, ctx: WorkBuddyDbContext, tenant_id: str, *, conn: Any | None = None
    ) -> dict[str, int]:
        """Delete every runtime row for a tenant (deletion lifecycle slice).

        Ledger tables keep their append-only guarantees during normal operation;
        tenant erasure is the single audited path that removes them.
        """
        tables = (
            "workbuddy_chat_messages",
            "workbuddy_chat_sessions",
            "workbuddy_notifications",
            "workbuddy_audit_logs",
            "workbuddy_quota_usage",
            "workbuddy_quota_reservations",
            "workbuddy_outbox",
            "workbuddy_jobs",
            "workbuddy_reconciliations",
            "workbuddy_approval_candidates",
            "workbuddy_approval_requests",
            "workbuddy_execution_payloads",
            "workbuddy_edge_runs",
            "workbuddy_step_runs",
            "workbuddy_executions",
            "workbuddy_leases",
        )
        deleted: dict[str, int] = {}
        with runtime_transaction(self._db, ctx, conn) as c:
            for table in tables:
                cursor = c.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))
                deleted[table] = int(getattr(cursor, "rowcount", 0) or 0)
        return deleted


__all__ = [
    "ApprovalCandidateRow",
    "ApprovalRequestRow",
    "AuditLogRow",
    "ChatMessageRow",
    "ChatSessionRow",
    "EdgeRunRow",
    "ExecutionRow",
    "JobRow",
    "LeaseRow",
    "NotificationRow",
    "OutboxRow",
    "PayloadRow",
    "QuotaReservationRow",
    "ReconciliationRow",
    "StepRunRow",
    "WorkBuddyPostgresRequiredError",
    "WorkBuddyRuntimeRepo",
    "canonical_json",
    "new_runtime_id",
    "require_postgres",
    "runtime_transaction",
]

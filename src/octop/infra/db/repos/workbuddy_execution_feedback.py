"""WorkBuddy correction/feedback ledger (PostgreSQL only).

A person who reads a settled run and sees that one value came back wrong -- a
total in the wrong column, a name the model normalised away -- records the
correction here instead of rewriting the execution, exactly as
``workbuddy_output_reviews`` records the judgement without touching the run.
The row carries the value that stood (``before``, NULL when the run produced
nothing at that key) and the value that takes its place (``after``), plus the
locked workflow version the correction was made against, so replaying the
correction later cannot be confused with one written for another version.

Every statement runs inside the shared audited transaction context, so tenant
isolation comes from the database (RLS + FORCE RLS bound to ``app.tenant_id``)
and never from request bodies; the rows are insert-only facts of the ledger and
the migration has no UPDATE or DELETE policy for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.workbuddy_runtime import (
    _jsonb,
    new_runtime_id,
    runtime_transaction,
)
from octop.infra.db.workbuddy_context import WorkBuddyDbContext


@dataclass(frozen=True, slots=True)
class ExecutionFeedbackRow:
    """One correction or supplied fact attached to an execution's output."""

    id: str
    tenant_id: str
    execution_id: str
    workflow_id: str
    workflow_version_id: str
    workflow_version_hash: str
    node_id: str | None
    output_key: str | None
    kind: str
    source: str
    source_id: str | None
    before: Any
    after: Any
    created_by_user_id: int | None
    created_at: Any

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExecutionFeedbackRow:
        return cls(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            execution_id=str(row["execution_id"]),
            workflow_id=str(row["workflow_id"]),
            workflow_version_id=str(row["workflow_version_id"]),
            workflow_version_hash=str(row["workflow_version_hash"]),
            node_id=(str(row["node_id"]) if row["node_id"] is not None else None),
            output_key=(str(row["output_key"]) if row["output_key"] is not None else None),
            kind=str(row["kind"]),
            source=str(row["source"]),
            source_id=(str(row["source_id"]) if row["source_id"] is not None else None),
            before=row["before"],
            after=row["after"],
            created_by_user_id=(
                int(row["created_by_user_id"]) if row["created_by_user_id"] is not None else None
            ),
            created_at=row["created_at"],
        )


class ExecutionFeedbackRepo:
    """Fact-store access for the corrections recorded against settled runs."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def insert_execution_feedback(
        self,
        ctx: WorkBuddyDbContext,
        *,
        tenant_id: str,
        execution_id: str,
        workflow_id: str,
        workflow_version_id: str,
        workflow_version_hash: str,
        kind: str,
        source: str,
        after: Any,
        node_id: str | None = None,
        output_key: str | None = None,
        source_id: str | None = None,
        before: Any = None,
        created_by_user_id: int | None = None,
        conn: Any | None = None,
    ) -> str:
        """Append one correction to the ledger and return its id.

        ``before`` stays NULL for a fact the run never produced -- a value the
        asker supplied rather than replaced -- so a reader can tell the two
        apart without inferring it from the value itself.
        """
        rid = new_runtime_id()
        with runtime_transaction(self._db, ctx, conn) as c:
            c.execute(
                """
                INSERT INTO workbuddy_execution_feedback(
                    id, tenant_id, execution_id, workflow_id, workflow_version_id,
                    workflow_version_hash, node_id, output_key, kind, source, source_id,
                    before, after, created_by_user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    tenant_id,
                    execution_id,
                    workflow_id,
                    workflow_version_id,
                    workflow_version_hash,
                    node_id,
                    output_key,
                    kind,
                    source,
                    source_id,
                    _jsonb(before),
                    _jsonb(after),
                    created_by_user_id,
                ),
            )
        return rid

    def list_execution_feedback(
        self,
        ctx: WorkBuddyDbContext,
        *,
        workflow_id: str,
        kind: str | None = None,
        since: float | None = None,
        limit: int = 200,
        conn: Any | None = None,
    ) -> list[ExecutionFeedbackRow]:
        """The newest corrections of one workflow, newest first.

        ``since`` is a unix epoch second: the caller pages the store by the
        timestamp it last read, so the boundary second is included whole.
        """
        clauses = ["f.tenant_id = ?", "f.workflow_id = ?"]
        params: list[Any] = [ctx.tenant_id, workflow_id]
        if kind is not None:
            clauses.append("f.kind = ?")
            params.append(kind)
        if since is not None:
            clauses.append("f.created_at >= to_timestamp(?)")
            params.append(float(since))
        params.append(max(1, min(int(limit), 200)))
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT f.* FROM workbuddy_execution_feedback f "
                f"WHERE {' AND '.join(clauses)} ORDER BY f.created_at DESC, f.id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [ExecutionFeedbackRow.from_row(r) for r in rows]

    def list_execution_feedback_for_execution(
        self,
        ctx: WorkBuddyDbContext,
        execution_id: str,
        *,
        conn: Any | None = None,
    ) -> list[ExecutionFeedbackRow]:
        """Every correction recorded against one execution, in write order."""
        with runtime_transaction(self._db, ctx, conn) as c:
            rows = c.execute(
                "SELECT * FROM workbuddy_execution_feedback WHERE execution_id = ? "
                "ORDER BY created_at, id",
                (execution_id,),
            ).fetchall()
        return [ExecutionFeedbackRow.from_row(r) for r in rows]


__all__ = [
    "ExecutionFeedbackRepo",
    "ExecutionFeedbackRow",
]

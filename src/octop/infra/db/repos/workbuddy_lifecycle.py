"""WorkBuddy tenant lifecycle persistence: exports, deletion, ledger, tombstones.

PostgreSQL only: every statement runs inside
:func:`octop.infra.db.workbuddy_context.workbuddy_transaction`, which fails
closed with ``WorkBuddyPostgresRequiredError`` on SQLite before a row is read.

This module is the SQL layer only — decisions (redaction rules, manifest shape,
policy gate, ledger hashing, purge ordering) live in
:mod:`octop.infra.workbuddy.lifecycle`.  Callers compose primitives inside
:meth:`WorkBuddyLifecycleRepo.transaction` so a purge is one atomic unit.

Guarantees this module keeps:

* redeem tokens are stored as sha256 only and consumed by a single conditional
  ``UPDATE ... WHERE consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now``
  so exactly one caller can win; re-authentication credentials follow the same
  rule (sha256 only, five-minute ceiling, consumed once);
* deletion request stage changes are compare-and-swap on ``version``;
* tenant row deletion is driven by an explicit, ordered table list, counts what
  it removed, and never touches the ledger, tombstones or other evidence tables;
* every tenant-scoped table is reached through RLS: platform maintenance runs in
  ``WorkBuddyDbContext.platform()``, tenant traffic never does.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import now_ts
from octop.infra.db.workbuddy_context import WorkBuddyDbContext, workbuddy_transaction

__all__ = ["WORKBUDDY_TABLE_PREFIX", "WorkBuddyLifecycleRepo"]

WORKBUDDY_TABLE_PREFIX = "workbuddy_"

# Tables whose rows belong to the tenant but must not be copied into an export.
_SQL_TENANT_TABLES = """
SELECT table_name FROM information_schema.columns
WHERE table_schema = current_schema() AND column_name = 'tenant_id' AND table_name LIKE ?
ORDER BY table_name
"""

_SQL_TABLE_COLUMNS = """
SELECT column_name FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name = ?
ORDER BY ordinal_position
"""

_SQL_TABLE_FKS = """
SELECT DISTINCT tc.table_name AS child, ccu.table_name AS parent
FROM information_schema.table_constraints AS tc
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name AND ccu.constraint_schema = tc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = current_schema()
"""


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return {str(key): row[key] for key in row}
    raise RuntimeError("row factory returned a non-mapping row")


def _rows(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [row for row in (_row_dict(item) for item in rows) if row is not None]


class WorkBuddyLifecycleRepo:
    """SQL primitives for the WorkBuddy tenant lifecycle."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    @contextmanager
    def transaction(self, ctx: WorkBuddyDbContext) -> Iterator[Any]:
        """One WorkBuddy transaction with the tenant/platform context applied."""
        with workbuddy_transaction(self._db, ctx) as conn:
            yield conn

    # ── schema introspection ────────────────────────────────────────────

    def tenant_tables(self, conn: Any, *, prefix: str = WORKBUDDY_TABLE_PREFIX) -> list[str]:
        """Tenant-scoped table names, ordered by name."""
        rows = conn.execute(_SQL_TENANT_TABLES, (f"{prefix}%",)).fetchall()
        return [str(row["table_name"]) for row in rows]

    def table_columns(self, conn: Any, table: str) -> list[str]:
        rows = conn.execute(_SQL_TABLE_COLUMNS, (table,)).fetchall()
        return [str(row["column_name"]) for row in rows]

    def table_dependencies(self, conn: Any) -> dict[str, set[str]]:
        """``child -> referenced parents`` for every foreign key in the schema."""
        deps: dict[str, set[str]] = {}
        for row in conn.execute(_SQL_TABLE_FKS).fetchall():
            deps.setdefault(str(row["child"]), set()).add(str(row["parent"]))
        return deps

    # ── export jobs ─────────────────────────────────────────────────────

    def insert_export_job(
        self,
        conn: Any,
        *,
        tenant_id: str,
        requested_by: int | None,
        redeem_expires_at: int,
        created_at: int,
        redaction_rules_version: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_export_jobs
              (tenant_id, requested_by, status, scope, redaction_rules_version,
               redeem_expires_at, created_at, updated_at)
            VALUES (?, ?, 'queued', 'tenant', ?, ?, ?, ?)
            RETURNING export_job_id, tenant_id, status, version, redeem_expires_at, created_at
            """,
            (
                tenant_id,
                requested_by,
                int(redaction_rules_version),
                int(redeem_expires_at),
                int(created_at),
                int(created_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("export job insert returned no row")
        return row

    def update_export_job_ready(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        expected_version: int,
        manifest_json: str,
        manifest_sha256: str,
        content_sha256: str,
        table_total: int,
        row_total: int,
        updated_at: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_export_jobs
               SET status = 'ready', manifest_json = ?, manifest_sha256 = ?, content_sha256 = ?,
                   table_total = ?, row_total = ?, completed_at = ?, updated_at = ?,
                   version = version + 1
             WHERE export_job_id = ? AND tenant_id = ? AND status = 'queued' AND version = ?
            """,
            (
                manifest_json,
                manifest_sha256,
                content_sha256,
                int(table_total),
                int(row_total),
                int(updated_at),
                int(updated_at),
                str(export_job_id),
                str(tenant_id),
                int(expected_version),
            ),
        )
        return int(cursor.rowcount) == 1

    def update_export_job_failed(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        failure_reason: str,
        updated_at: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_export_jobs
               SET status = 'failed', failure_reason = ?, updated_at = ?, version = version + 1
             WHERE export_job_id = ? AND tenant_id = ? AND status = 'queued'
            """,
            (failure_reason, int(updated_at), str(export_job_id), str(tenant_id)),
        )
        return int(cursor.rowcount) == 1

    def get_export_job(
        self, conn: Any, *, tenant_id: str, export_job_id: str
    ) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                "SELECT * FROM workbuddy_export_jobs WHERE export_job_id = ? AND tenant_id = ?",
                (str(export_job_id), str(tenant_id)),
            ).fetchone()
        )

    def list_export_jobs(
        self, conn: Any, *, tenant_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT * FROM workbuddy_export_jobs
                WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?
                """,
                (str(tenant_id), int(limit)),
            ).fetchall()
        )

    def insert_export_artifact(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        table_name: str,
        row_count: int,
        content_sha256: str,
        payload_json: str,
        created_at: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO workbuddy_export_artifacts
              (tenant_id, export_job_id, table_name, row_count, content_sha256, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(tenant_id),
                str(export_job_id),
                str(table_name),
                int(row_count),
                content_sha256,
                payload_json,
                int(created_at),
            ),
        )

    def list_export_artifacts(
        self, conn: Any, *, tenant_id: str, export_job_id: str
    ) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT table_name, row_count, content_sha256, payload_json
                FROM workbuddy_export_artifacts
                WHERE tenant_id = ? AND export_job_id = ? ORDER BY table_name
                """,
                (str(tenant_id), str(export_job_id)),
            ).fetchall()
        )

    def fetch_tenant_rows(
        self, conn: Any, *, table: str, columns: Sequence[str], limit: int
    ) -> list[dict[str, Any]]:
        """Read one tenant table (RLS restricts the rows) with a hard row ceiling."""
        if not columns:
            return []
        projection = ", ".join(_quote_ident(column) for column in columns)
        return _rows(
            conn.execute(
                f"SELECT {projection} FROM {_quote_ident(table)} LIMIT ?",
                (int(limit) + 1,),
            ).fetchall()
        )

    def expire_export_jobs(
        self, conn: Any, *, now: int, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Drop payloads past their redeem window, keep the manifest as evidence."""
        where = "redeem_expires_at <= ? AND status IN ('ready', 'redeemed')"
        params: list[Any] = [int(now)]
        if tenant_id is not None:
            where += " AND tenant_id = ?"
            params.append(str(tenant_id))
        jobs = _rows(
            conn.execute(
                f"SELECT export_job_id, tenant_id FROM workbuddy_export_jobs WHERE {where}",
                tuple(params),
            ).fetchall()
        )
        for job in jobs:
            conn.execute(
                """
                DELETE FROM workbuddy_export_artifacts
                WHERE tenant_id = ? AND export_job_id = ?
                """,
                (job["tenant_id"], job["export_job_id"]),
            )
            conn.execute(
                """
                UPDATE workbuddy_export_redeem_tokens
                   SET revoked_at = ?
                 WHERE tenant_id = ? AND export_job_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                """,
                (int(now), job["tenant_id"], job["export_job_id"]),
            )
            conn.execute(
                """
                UPDATE workbuddy_export_jobs
                   SET status = 'expired', updated_at = ?, version = version + 1
                 WHERE tenant_id = ? AND export_job_id = ?
                """,
                (int(now), job["tenant_id"], job["export_job_id"]),
            )
        return jobs

    # ── redeem tokens ───────────────────────────────────────────────────

    def insert_redeem_token(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        token_sha256: str,
        issued_by: int | None,
        issued_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_export_redeem_tokens
              (tenant_id, export_job_id, token_sha256, issued_by, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING redeem_token_id, expires_at
            """,
            (
                str(tenant_id),
                str(export_job_id),
                token_sha256,
                issued_by,
                int(issued_at),
                int(expires_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("redeem token insert returned no row")
        return row

    def revoke_live_redeem_tokens(
        self, conn: Any, *, tenant_id: str, export_job_id: str, revoked_at: int
    ) -> int:
        cursor = conn.execute(
            """
            UPDATE workbuddy_export_redeem_tokens SET revoked_at = ?
             WHERE tenant_id = ? AND export_job_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
            """,
            (int(revoked_at), str(tenant_id), str(export_job_id)),
        )
        return int(cursor.rowcount)

    def get_redeem_token(
        self, conn: Any, *, tenant_id: str, token_sha256: str
    ) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM workbuddy_export_redeem_tokens
                WHERE tenant_id = ? AND token_sha256 = ?
                """,
                (str(tenant_id), token_sha256),
            ).fetchone()
        )

    def consume_redeem_token(
        self,
        conn: Any,
        *,
        tenant_id: str,
        token_sha256: str,
        consumed_by: int | None,
        consumed_at: int,
    ) -> bool:
        """Single conditional UPDATE: exactly one of N racing callers wins."""
        cursor = conn.execute(
            """
            UPDATE workbuddy_export_redeem_tokens
               SET consumed_at = ?, consumed_by = ?
             WHERE tenant_id = ? AND token_sha256 = ?
               AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
            """,
            (int(consumed_at), consumed_by, str(tenant_id), token_sha256, int(consumed_at)),
        )
        return int(cursor.rowcount) == 1

    def mark_export_redeemed(
        self,
        conn: Any,
        *,
        tenant_id: str,
        export_job_id: str,
        redeemed_by: int | None,
        redeemed_at: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_export_jobs
               SET status = 'redeemed', redeemed_at = ?, redeemed_by = ?, updated_at = ?,
                   version = version + 1
             WHERE tenant_id = ? AND export_job_id = ? AND status = 'ready'
            """,
            (int(redeemed_at), redeemed_by, int(redeemed_at), str(tenant_id), str(export_job_id)),
        )
        return int(cursor.rowcount) == 1

    # ── re-authentication credentials ───────────────────────────────────

    def insert_reauth_credential(
        self,
        conn: Any,
        *,
        tenant_id: str,
        user_id: int,
        purpose: str,
        credential_sha256: str,
        issued_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_reauth_credentials
              (tenant_id, user_id, purpose, credential_sha256, issued_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING reauth_credential_id, expires_at
            """,
            (
                str(tenant_id),
                int(user_id),
                str(purpose),
                credential_sha256,
                int(issued_at),
                int(expires_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("re-authentication credential insert returned no row")
        return row

    def get_reauth_credential(
        self, conn: Any, *, tenant_id: str, credential_sha256: str
    ) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM workbuddy_reauth_credentials
                WHERE tenant_id = ? AND credential_sha256 = ?
                """,
                (str(tenant_id), credential_sha256),
            ).fetchone()
        )

    def consume_reauth_credential(
        self, conn: Any, *, tenant_id: str, credential_sha256: str, consumed_at: int
    ) -> bool:
        """Single conditional UPDATE: exactly one of N racing callers wins."""
        cursor = conn.execute(
            """
            UPDATE workbuddy_reauth_credentials
               SET consumed_at = ?
             WHERE tenant_id = ? AND credential_sha256 = ?
               AND consumed_at IS NULL AND expires_at > ?
            """,
            (int(consumed_at), str(tenant_id), credential_sha256, int(consumed_at)),
        )
        return int(cursor.rowcount) == 1

    # ── deletion requests ───────────────────────────────────────────────

    def insert_deletion_request(
        self,
        conn: Any,
        *,
        tenant_id: str,
        requested_by: int | None,
        requested_at: int,
        cooling_off_ends_at: int,
        policy_sha256: str,
        policy_expires_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_deletion_requests
              (tenant_id, stage, requested_by, requested_at, cooling_off_ends_at,
               policy_sha256, policy_expires_at, updated_at)
            VALUES (?, 'cooling_off', ?, ?, ?, ?, ?, ?)
            RETURNING deletion_request_id, tenant_id, stage, version, requested_at,
                      cooling_off_ends_at, policy_sha256, policy_expires_at,
                      purge_due_at, purged_at
            """,
            (
                str(tenant_id),
                requested_by,
                int(requested_at),
                int(cooling_off_ends_at),
                policy_sha256,
                int(policy_expires_at),
                int(requested_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("deletion request insert returned no row")
        return row

    def get_deletion_request(
        self, conn: Any, *, tenant_id: str, deletion_request_id: str
    ) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM workbuddy_deletion_requests
                WHERE deletion_request_id = ? AND tenant_id = ?
                """,
                (str(deletion_request_id), str(tenant_id)),
            ).fetchone()
        )

    def find_active_deletion_request(self, conn: Any, *, tenant_id: str) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM workbuddy_deletion_requests
                WHERE tenant_id = ? AND stage IN ('cooling_off', 'archived')
                ORDER BY requested_at DESC LIMIT 1
                """,
                (str(tenant_id),),
            ).fetchone()
        )

    def list_deletion_requests(
        self, conn: Any, *, tenant_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT * FROM workbuddy_deletion_requests
                WHERE tenant_id = ? ORDER BY requested_at DESC LIMIT ?
                """,
                (str(tenant_id), int(limit)),
            ).fetchall()
        )

    def cancel_deletion_request(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str,
        expected_version: int,
        cancelled_by: int | None,
        cancelled_at: int,
    ) -> bool:
        """CAS cancel that only matches a live cooling-off request."""
        cursor = conn.execute(
            """
            UPDATE workbuddy_deletion_requests
               SET stage = 'cancelled', cancelled_at = ?, cancelled_by = ?, updated_at = ?,
                   version = version + 1
             WHERE deletion_request_id = ? AND tenant_id = ? AND stage = 'cooling_off'
               AND version = ? AND cooling_off_ends_at > ?
            """,
            (
                int(cancelled_at),
                cancelled_by,
                int(cancelled_at),
                str(deletion_request_id),
                str(tenant_id),
                int(expected_version),
                int(cancelled_at),
            ),
        )
        return int(cursor.rowcount) == 1

    def due_deletion_requests(self, conn: Any, *, now: int) -> list[dict[str, Any]]:
        """Requests whose cooling-off ended (archive) or whose retention expired (purge)."""
        return _rows(
            conn.execute(
                """
                SELECT * FROM workbuddy_deletion_requests
                WHERE (stage = 'cooling_off' AND cooling_off_ends_at <= ?)
                   OR (stage = 'archived' AND purge_due_at IS NOT NULL AND purge_due_at <= ?)
                ORDER BY requested_at
                """,
                (int(now), int(now)),
            ).fetchall()
        )

    def mark_deletion_archived(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str,
        expected_version: int,
        archived_at: int,
        archive_sha256: str,
        archive_row_total: int,
        purge_due_at: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_deletion_requests
               SET stage = 'archived', archived_at = ?, archive_sha256 = ?, archive_row_total = ?,
                   purge_due_at = ?, updated_at = ?, version = version + 1
             WHERE deletion_request_id = ? AND tenant_id = ? AND stage = 'cooling_off' AND version = ?
            """,
            (
                int(archived_at),
                archive_sha256,
                int(archive_row_total),
                int(purge_due_at),
                int(archived_at),
                str(deletion_request_id),
                str(tenant_id),
                int(expected_version),
            ),
        )
        return int(cursor.rowcount) == 1

    def mark_deletion_purged(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str,
        expected_version: int,
        purged_at: int,
        ledger_sequence: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_deletion_requests
               SET stage = 'purged', purged_at = ?, purged_ledger_sequence = ?, updated_at = ?,
                   version = version + 1
             WHERE deletion_request_id = ? AND tenant_id = ? AND stage = 'archived' AND version = ?
            """,
            (
                int(purged_at),
                int(ledger_sequence),
                int(purged_at),
                str(deletion_request_id),
                str(tenant_id),
                int(expected_version),
            ),
        )
        return int(cursor.rowcount) == 1

    # ── legal holds ─────────────────────────────────────────────────────

    def insert_legal_hold(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str | None,
        matter_reference: str,
        reason: str,
        placed_by: int | None,
        placed_by_label: str | None,
        placed_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_legal_holds
              (tenant_id, deletion_request_id, matter_reference, reason, placed_by,
               placed_by_label, placed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING legal_hold_id, tenant_id, deletion_request_id, placed_at, released_at
            """,
            (
                str(tenant_id),
                None if deletion_request_id is None else str(deletion_request_id),
                matter_reference,
                reason,
                placed_by,
                placed_by_label,
                int(placed_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("legal hold insert returned no row")
        return row

    def release_legal_hold(
        self,
        conn: Any,
        *,
        tenant_id: str,
        legal_hold_id: str,
        released_by: int | None,
        release_reason: str,
        released_at: int,
    ) -> bool:
        cursor = conn.execute(
            """
            UPDATE workbuddy_legal_holds
               SET released_at = ?, released_by = ?, release_reason = ?
             WHERE legal_hold_id = ? AND tenant_id = ? AND released_at IS NULL
            """,
            (
                int(released_at),
                released_by,
                release_reason,
                str(legal_hold_id),
                str(tenant_id),
            ),
        )
        return int(cursor.rowcount) == 1

    def count_active_legal_holds(
        self, conn: Any, *, tenant_id: str, deletion_request_id: str | None = None
    ) -> int:
        params: list[Any] = [str(tenant_id)]
        where = "tenant_id = ? AND released_at IS NULL"
        if deletion_request_id is not None:
            where += " AND (deletion_request_id = ? OR deletion_request_id IS NULL)"
            params.append(str(deletion_request_id))
        row = conn.execute(
            f"SELECT count(*) AS total FROM workbuddy_legal_holds WHERE {where}", tuple(params)
        ).fetchone()
        return int((_row_dict(row) or {"total": 0})["total"])

    def list_legal_holds(
        self, conn: Any, *, tenant_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT * FROM workbuddy_legal_holds
                WHERE tenant_id = ? ORDER BY placed_at DESC LIMIT ?
                """,
                (str(tenant_id), int(limit)),
            ).fetchall()
        )

    # ── archives ────────────────────────────────────────────────────────

    def insert_archive(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str,
        archive_sha256: str,
        table_total: int,
        row_total: int,
        payload_json: str,
        retained_until: int,
        created_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_tenant_archives
              (tenant_id, deletion_request_id, archive_sha256, table_total, row_total,
               payload_json, retained_until, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING archive_id, tenant_id, deletion_request_id, retained_until
            """,
            (
                str(tenant_id),
                str(deletion_request_id),
                archive_sha256,
                int(table_total),
                int(row_total),
                payload_json,
                int(retained_until),
                int(created_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("archive insert returned no row")
        return row

    def get_archive(
        self, conn: Any, *, tenant_id: str, deletion_request_id: str
    ) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT * FROM workbuddy_tenant_archives
                WHERE tenant_id = ? AND deletion_request_id = ?
                """,
                (str(tenant_id), str(deletion_request_id)),
            ).fetchone()
        )

    def clear_archive_payload(
        self, conn: Any, *, tenant_id: str, deletion_request_id: str, purged_at: int
    ) -> int:
        cursor = conn.execute(
            """
            UPDATE workbuddy_tenant_archives
               SET payload_json = '[]', purged_at = ?
             WHERE tenant_id = ? AND deletion_request_id = ? AND purged_at IS NULL
            """,
            (int(purged_at), str(tenant_id), str(deletion_request_id)),
        )
        return int(cursor.rowcount)

    # ── deletion ledger ─────────────────────────────────────────────────

    def ledger_head(self, conn: Any, *, tenant_id: str) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT sequence, entry_sha256, created_at FROM workbuddy_deletion_ledger
                WHERE tenant_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (str(tenant_id),),
            ).fetchone()
        )

    def count_ledger_entries(self, conn: Any, *, tenant_id: str) -> int:
        row = conn.execute(
            "SELECT count(*) AS total FROM workbuddy_deletion_ledger WHERE tenant_id = ?",
            (str(tenant_id),),
        ).fetchone()
        return int((_row_dict(row) or {"total": 0})["total"])

    def list_ledger_entries(
        self, conn: Any, *, tenant_id: str, limit: int = 200, after_sequence: int = 0
    ) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT * FROM workbuddy_deletion_ledger
                WHERE tenant_id = ? AND sequence > ? ORDER BY sequence LIMIT ?
                """,
                (str(tenant_id), int(after_sequence), int(limit)),
            ).fetchall()
        )

    def insert_ledger_entry(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str | None,
        sequence: int,
        entry_type: str,
        payload_json: str,
        payload_sha256: str,
        previous_sha256: str | None,
        entry_sha256: str,
        actor_user_id: int | None,
        actor_label: str | None,
        created_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_deletion_ledger
              (tenant_id, deletion_request_id, sequence, entry_type, payload_json, payload_sha256,
               previous_sha256, entry_sha256, actor_user_id, actor_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING ledger_entry_id, sequence, entry_sha256
            """,
            (
                str(tenant_id),
                None if deletion_request_id is None else str(deletion_request_id),
                int(sequence),
                entry_type,
                payload_json,
                payload_sha256,
                previous_sha256,
                entry_sha256,
                actor_user_id,
                actor_label,
                int(created_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("ledger insert returned no row")
        return row

    # ── tombstones ──────────────────────────────────────────────────────

    def get_tombstone(self, conn: Any, *, tenant_id: str) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                "SELECT * FROM workbuddy_tenant_tombstones WHERE tenant_id = ?",
                (str(tenant_id),),
            ).fetchone()
        )

    def insert_tombstone(
        self,
        conn: Any,
        *,
        tenant_id: str,
        deletion_request_id: str,
        purged_at: int,
        policy_sha256: str,
        ledger_head_sha256: str,
        ledger_entry_count: int,
        archive_sha256: str | None,
        archive_row_total: int | None,
        usage_linkage_sha256: str | None,
        purged_tables: int,
        purged_rows: int,
        retained_evidence: str,
        tombstone_sha256: str,
        created_at: int,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            INSERT INTO workbuddy_tenant_tombstones
              (tenant_id, deletion_request_id, purged_at, policy_sha256, ledger_head_sha256,
               ledger_entry_count, archive_sha256, archive_row_total, usage_linkage_sha256,
               purged_tables, purged_rows, retained_evidence, tombstone_sha256, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING tenant_id, purged_at, tombstone_sha256
            """,
            (
                str(tenant_id),
                str(deletion_request_id),
                int(purged_at),
                policy_sha256,
                ledger_head_sha256,
                int(ledger_entry_count),
                archive_sha256,
                archive_row_total,
                usage_linkage_sha256,
                int(purged_tables),
                int(purged_rows),
                retained_evidence,
                tombstone_sha256,
                int(created_at),
            ),
        ).fetchone()
        row = _row_dict(row)
        if row is None:
            raise RuntimeError("tombstone insert returned no row")
        return row

    def list_tombstones(self, conn: Any, *, limit: int = 200) -> list[dict[str, Any]]:
        return _rows(
            conn.execute(
                """
                SELECT tenant_id, deletion_request_id, purged_at, ledger_head_sha256, tombstone_sha256
                FROM workbuddy_tenant_tombstones ORDER BY purged_at DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        )

    # ── purge and coverage ──────────────────────────────────────────────

    def delete_tenant_rows(
        self, conn: Any, *, tenant_id: str, tables: Sequence[str]
    ) -> dict[str, int]:
        """Delete every row of ``tenant_id`` in ``tables`` (children first) and count it."""
        deleted: dict[str, int] = {}
        for table in tables:
            cursor = conn.execute(
                f"DELETE FROM {_quote_ident(table)} WHERE tenant_id = ?", (str(tenant_id),)
            )
            deleted[str(table)] = int(cursor.rowcount)
        return deleted

    def delete_tenant_rows_batched(
        self, conn: Any, *, tenant_id: str, tables: Sequence[str]
    ) -> dict[str, int]:
        """Empty cyclic tables in one statement and count what each one dropped.

        A foreign-key cycle cannot be emptied table by table: whichever table
        went first would still be referenced by the other.  PostgreSQL checks
        immediate constraints only once the whole statement is done, so removing
        the group together leaves nothing to violate.  Data-modifying CTEs run
        exactly once whether or not the final query reads them, which is what
        makes the per-table counts observable here.
        """
        if not tables:
            return {}
        deletes = ", ".join(
            f"d{index} AS (DELETE FROM {_quote_ident(table)} WHERE tenant_id = ? RETURNING 1)"
            for index, table in enumerate(tables)
        )
        projection = ", ".join(
            f"(SELECT count(*) FROM d{index}) AS c{index}" for index in range(len(tables))
        )
        row = _row_dict(
            conn.execute(
                f"WITH {deletes} SELECT {projection}",
                tuple(str(tenant_id) for _ in tables),
            ).fetchone()
        )
        counts = row or {}
        return {str(table): int(counts[f"c{index}"]) for index, table in enumerate(tables)}

    def tenant_row_counts(
        self, conn: Any, *, tenant_id: str, tables: Sequence[str]
    ) -> dict[str, int]:
        """Rows still present per table — the post-purge (and post-restore) coverage check."""
        counts: dict[str, int] = {}
        for table in tables:
            row = conn.execute(
                f"SELECT count(*) AS total FROM {_quote_ident(table)} WHERE tenant_id = ?",
                (str(tenant_id),),
            ).fetchone()
            counts[str(table)] = int((_row_dict(row) or {"total": 0})["total"])
        return counts

    # ── anonymized usage linkage ────────────────────────────────────────

    def insert_usage_salt(
        self, conn: Any, *, tenant_id: str, salt_ref: str, salt_secret: bytes, created_at: int
    ) -> None:
        conn.execute(
            """
            INSERT INTO workbuddy_usage_salts (tenant_id, salt_ref, salt_secret, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(tenant_id), salt_ref, salt_secret, int(created_at)),
        )

    def live_usage_salt(self, conn: Any, *, tenant_id: str) -> dict[str, Any] | None:
        return _row_dict(
            conn.execute(
                """
                SELECT salt_ref, salt_secret FROM workbuddy_usage_salts
                WHERE tenant_id = ? AND destroyed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(tenant_id),),
            ).fetchone()
        )

    def upsert_usage_link(
        self,
        conn: Any,
        *,
        tenant_id: str,
        salt_ref: str,
        subject_kind: str,
        subject_sha256: str,
        seen_at: int,
        event_count: int = 1,
    ) -> None:
        conn.execute(
            """
            INSERT INTO workbuddy_anonymized_usage_links
              (tenant_id, salt_ref, subject_kind, subject_sha256, event_count, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, subject_kind, subject_sha256) DO UPDATE
              SET event_count = workbuddy_anonymized_usage_links.event_count + EXCLUDED.event_count,
                  last_seen_at = EXCLUDED.last_seen_at
            """,
            (
                str(tenant_id),
                salt_ref,
                subject_kind,
                subject_sha256,
                int(event_count),
                int(seen_at),
                int(seen_at),
            ),
        )

    def destroy_usage_salts(self, conn: Any, *, tenant_id: str, destroyed_at: int) -> int:
        """Wipe the hashing key material so stored subject hashes stop being linkable."""
        cursor = conn.execute(
            """
            UPDATE workbuddy_usage_salts
               SET salt_secret = ''::bytea, destroyed_at = ?
             WHERE tenant_id = ? AND destroyed_at IS NULL
            """,
            (int(destroyed_at), str(tenant_id)),
        )
        return int(cursor.rowcount)

    def mark_usage_links_purged(self, conn: Any, *, tenant_id: str, purged_at: int) -> int:
        cursor = conn.execute(
            """
            UPDATE workbuddy_anonymized_usage_links SET purged_at = ?
             WHERE tenant_id = ? AND purged_at IS NULL
            """,
            (int(purged_at), str(tenant_id)),
        )
        return int(cursor.rowcount)

    def usage_linkage_digest_input(self, conn: Any, *, tenant_id: str) -> dict[str, Any]:
        """Aggregate view of the anonymized linkage, used for the tombstone digest."""
        rows = _rows(
            conn.execute(
                """
                SELECT subject_kind, subject_sha256, event_count
                FROM workbuddy_anonymized_usage_links
                WHERE tenant_id = ? ORDER BY subject_kind, subject_sha256
                """,
                (str(tenant_id),),
            ).fetchall()
        )
        return {"subjects": rows, "total_events": sum(int(row["event_count"]) for row in rows)}

    # ── misc ────────────────────────────────────────────────────────────

    def tenant_exists(self, conn: Any, *, tenant_id: str) -> bool:
        row = conn.execute(
            "SELECT 1 AS present FROM workbuddy_tenants WHERE tenant_id = ?", (str(tenant_id),)
        ).fetchone()
        return row is not None

    def now(self) -> int:
        return now_ts()

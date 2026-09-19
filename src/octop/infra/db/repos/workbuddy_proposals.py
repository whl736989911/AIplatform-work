"""WorkBuddy improvement-proposal persistence (PostgreSQL only).

Every public WorkBuddy identifier is a UUID string; Octop user references stay
integer ``users.id`` values and the acting membership is the tenant membership
UUID.  Timestamps are unix epoch seconds in the proposal tables (matching the
Octop control plane) while the workflow tables keep ``timestamptz``.

Guarantees enforced here on top of ``020_workbuddy_proposals.pg.sql``:

* tenant isolation through the transaction-local ``app.*`` settings and FORCE
  RLS — scoped misses and cross-tenant references are indistinguishable;
* the base version id + content hash and the immutable candidate version are
  fixed in one transaction that locks the workflow row, applies the RFC 6902
  patch to the canonical base and re-diffs the final document, so a patch cannot
  smuggle in a boundary change between validation and insert;
* one pending proposal per workflow (partial unique index) and compare-and-swap
  updates keyed on ``(status, workflow_revision)``: apply/abort/rollback races
  serialise on the workflow revision and losers are refused, not retried;
* applying a candidate inserts a new ``origin = 'promotion'`` workflow version
  and moves ``active_version_id`` in the same transaction as the proposal
  status, while shadow/canary traffic pins ``shadow_version_id`` without
  touching the revision;
* reviews, shadow runs and canary evaluations are append-only evidence rows.

Failures raise :class:`WorkBuddyError` (a ``ValueError``) carrying a stable
English code, or :class:`ProposalConflictError` / :class:`ProposalNotFoundError`
from the proposals domain; the API layer maps the code to its HTTP response.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.db.repos.workbuddy_identity import WorkBuddyError
from octop.infra.db.workbuddy_context import (
    WorkBuddyDbContext,
    coerce_workbuddy_context,
    normalize_user_id,
    normalize_uuid,
    require_postgres,
    workbuddy_transaction,
)
from octop.infra.workbuddy.proposals import (
    CompiledProposal,
    CompileFn,
    EvaluationRow,
    GateVerdict,
    NewEvaluation,
    NewProposal,
    NewReview,
    PhaseMetrics,
    ProposalConflictError,
    ProposalNotFoundError,
    ProposalRecord,
    ProposalStatus,
    ReviewDecision,
    ReviewRecord,
    SemanticChange,
    ShadowRunRow,
    WorkflowPointer,
    definition_hash,
)

ERROR_POSTGRES_REQUIRED = "DEPENDENCY_UNAVAILABLE"
ERROR_CONTEXT_INVALID = "WORKBUDDY_CONTEXT_INVALID"
ERROR_INVALID_ARGUMENT = "WORKBUDDY_INVALID_ARGUMENT"
ERROR_MEMBERSHIP_REQUIRED = "FORBIDDEN_ROLE"
ERROR_WORKFLOW_INVALID = "WF_INVALID_SCHEMA"
ERROR_BASE_CONFLICT = "PROPOSAL_BASE_CONFLICT"
ERROR_CANDIDATE_EXISTS = "PROPOSAL_CANDIDATE_EXISTS"
ERROR_DUPLICATE_REVIEW = "APPROVAL_ALREADY_DECIDED"
ERROR_STATE_CONFLICT = "STATE_CONFLICT"
ERROR_VERSION_IMMUTABLE = "WORKBUDDY_VERSION_IMMUTABLE"

MAX_LIST_LIMIT = 200
DEFAULT_LIST_LIMIT = 50

_STATUS_VALUES = tuple(status.value for status in ProposalStatus)
_TERMINAL_STATUSES = ("applied", "rejected", "aborted", "superseded", "stale")
_OPEN_STATUSES = ("under_review", "approved", "shadow", "canary")
_PENDING_STATUSES = ("under_review", "approved")
_PROMOTION_FIELDS = {
    "status_reason",
    "canary_ratio_bp",
    "canary_started_at",
    "canary_stopped_at",
    "canary_stop_reason",
    "applied_version_id",
    "applied_at",
}

_PROPOSAL_COLUMNS = (
    "proposal_id, workflow_id, workflow_revision, base_version_id, base_content_hash,"
    " candidate_version_id, candidate_content_hash, status, status_reason, risk_level,"
    " pii_involved, required_approvals, requires_manual_shadow, change_summary,"
    " semantic_changes, created_by_user_id, created_by_membership_id, canary_ratio_bp,"
    " canary_started_at, canary_stopped_at, canary_stop_reason, applied_version_id,"
    " created_at, updated_at"
)


def _as_json(value: Any) -> Any:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return value


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _is_unique_violation(exc: Exception) -> bool:
    sqlstate = getattr(exc, "sqlstate", None) or getattr(
        getattr(exc, "diag", None), "sqlstate", None
    )
    if sqlstate == "23505":
        return True
    cause = exc.__cause__
    if isinstance(cause, Exception):
        return _is_unique_violation(cause)
    return "23505" in str(exc) or "duplicate key value" in str(exc)


def _int(value: Any) -> int:
    return int(value) if value is not None else 0


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


class WorkBuddyProposalsRepo:
    """Improvement-proposal persistence and promotion compare-and-swap.

    Construction is cheap (the pool is stored, nothing is checked out) but fails
    closed on non-PostgreSQL pools, so building it per request is fine. Each
    method owns its transaction; pass ``conn=`` to join an ambient
    :func:`~octop.infra.db.workbuddy_context.workbuddy_transaction` instead.
    """

    def __init__(self, db: DatabasePool, ctx: Any = None) -> None:
        require_postgres(db)
        self._db = db
        self._ctx: WorkBuddyDbContext = coerce_workbuddy_context(ctx)

    # ── context helpers ──────────────────────────────────────────────────────

    def _require_tenant(self) -> str:
        tenant_id = self._ctx.tenant_id
        if not tenant_id:
            raise WorkBuddyError(
                ERROR_CONTEXT_INVALID, "a tenant context is required for improvement proposals"
            )
        return tenant_id

    def _require_membership(self, value: object) -> str:
        membership_id = normalize_uuid(value, field="membership_id") if value else None
        if not membership_id:
            raise WorkBuddyError(
                ERROR_MEMBERSHIP_REQUIRED, "an active tenant membership is required"
            )
        return membership_id

    @contextmanager
    def _transaction(self, conn: Any = None) -> Iterator[Any]:
        if conn is not None:
            yield conn
            return
        with workbuddy_transaction(self._db, self._ctx) as ambient:
            yield ambient

    @staticmethod
    def _all(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[DbRow]:
        return list(conn.execute(sql, tuple(params)).fetchall())

    @staticmethod
    def _one(conn: Any, sql: str, params: Sequence[Any] = ()) -> DbRow | None:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    # ── proposals ────────────────────────────────────────────────────────────

    def create_proposal(
        self, request: NewProposal, *, compile: CompileFn, conn: Any = None
    ) -> ProposalRecord:
        """Compile the patch against the locked active version and store it.

        The workflow row is locked for the whole call, so the base version and
        its hash cannot change between the read, the compile and the insert.
        """
        tenant_id = self._require_tenant()
        workflow_id = normalize_uuid(request.workflow_id, field="workflow_id")
        created_by = normalize_user_id(request.actor.user_id)
        membership_id = self._require_membership(request.actor.membership_id)
        expected_revision = int(request.expect_revision)
        if expected_revision < 0:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "expect_revision must not be negative")
        now = now_ts()
        with self._transaction(conn) as ambient:
            workflow = self._one(
                ambient,
                "SELECT revision, active_version_id, status FROM workbuddy_workflows"
                " WHERE tenant_id = ? AND workflow_id = ? FOR UPDATE",
                (tenant_id, workflow_id),
            )
            if workflow is None:
                raise ProposalNotFoundError(f"workflow {workflow_id} is not visible")
            if str(workflow["status"]) != "active":
                raise WorkBuddyError(ERROR_WORKFLOW_INVALID, "workflow is not active")
            revision = _int(workflow["revision"])
            if revision != expected_revision:
                raise ProposalConflictError(
                    ERROR_BASE_CONFLICT, "the workflow revision changed; reload and retry"
                )
            base_version_id = workflow["active_version_id"]
            if base_version_id is None:
                raise WorkBuddyError(ERROR_WORKFLOW_INVALID, "workflow has no active version")
            base_version_id = str(base_version_id)
            base_row = self._one(
                ambient,
                "SELECT definition, definition_sha256 FROM workbuddy_workflow_versions"
                " WHERE tenant_id = ? AND workflow_id = ? AND workflow_version_id = ?",
                (tenant_id, workflow_id, base_version_id),
            )
            if base_row is None:
                raise WorkBuddyError(
                    ERROR_WORKFLOW_INVALID, "the active workflow version is missing"
                )
            base_definition = _as_json(base_row["definition"])
            base_hash = str(base_row["definition_sha256"])
            if definition_hash(base_definition) != base_hash:
                raise ProposalConflictError(
                    ERROR_BASE_CONFLICT, "the stored base definition does not match its hash"
                )
            compiled: CompiledProposal = compile(base_definition)
            if compiled.base_content_hash != base_hash:
                raise ProposalConflictError(
                    ERROR_BASE_CONFLICT, "the proposal base is not the active workflow version"
                )
            existing = self._one(
                ambient,
                "SELECT proposal_id FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND workflow_id = ? AND status IN ('under_review','approved')"
                " LIMIT 1",
                (tenant_id, workflow_id),
            )
            if existing is not None:
                raise ProposalConflictError(
                    ERROR_CANDIDATE_EXISTS, "the workflow already has a pending proposal"
                )
            candidate_version_id = str(uuid.uuid4())
            version_number = self._next_version_number(ambient, tenant_id, workflow_id)
            self._insert_version(
                ambient,
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                version_id=candidate_version_id,
                version_number=version_number,
                definition=compiled.candidate_definition,
                content_hash=compiled.candidate_content_hash,
                origin="proposal",
                base_version_id=base_version_id,
                source_version_id=base_version_id,
                change_summary=request.change_summary,
                created_by=created_by,
                created_by_membership_id=membership_id,
                created_at=now,
            )
            proposal_id = str(uuid.uuid4())
            try:
                ambient.execute(
                    "INSERT INTO workbuddy_improvement_proposals ("
                    " tenant_id, proposal_id, workflow_id, workflow_revision, base_version_id,"
                    " base_content_hash, candidate_version_id, candidate_content_hash, status,"
                    " status_reason, risk_level, pii_involved, required_approvals,"
                    " requires_manual_shadow, change_summary, source_patch, semantic_changes,"
                    " created_by_user_id, created_by_membership_id, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        proposal_id,
                        workflow_id,
                        revision,
                        base_version_id,
                        base_hash,
                        candidate_version_id,
                        compiled.candidate_content_hash,
                        ProposalStatus.UNDER_REVIEW.value,
                        None,
                        compiled.risk.level,
                        compiled.risk.pii,
                        compiled.risk.required_approvals,
                        compiled.risk.requires_manual_shadow,
                        request.change_summary,
                        _dump(list(request.patch)),
                        _dump([change.to_dict() for change in compiled.changes]),
                        created_by,
                        membership_id,
                        now,
                        now,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - translated to a typed conflict
                if _is_unique_violation(exc):
                    raise ProposalConflictError(
                        ERROR_CANDIDATE_EXISTS, "the workflow already has a pending proposal"
                    ) from exc
                raise
            row = self._one(
                ambient,
                f"SELECT {_PROPOSAL_COLUMNS} FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND proposal_id = ?",
                (tenant_id, proposal_id),
            )
            if row is None:  # pragma: no cover - insert just succeeded
                raise ProposalConflictError(ERROR_STATE_CONFLICT, "proposal insert was not visible")
            return _proposal_from_row(row)

    def get_proposal(self, proposal_id: str, *, conn: Any = None) -> ProposalRecord | None:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        with self._transaction(conn) as ambient:
            row = self._one(
                ambient,
                f"SELECT {_PROPOSAL_COLUMNS} FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND proposal_id = ?",
                (tenant_id, public_id),
            )
            return _proposal_from_row(row) if row is not None else None

    def list_proposals(
        self,
        *,
        workflow_id: str | None = None,
        status: ProposalStatus | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
        conn: Any = None,
    ) -> list[ProposalRecord]:
        tenant_id = self._require_tenant()
        if limit < 1 or limit > MAX_LIST_LIMIT:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "limit must be within 1..200")
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(normalize_uuid(workflow_id, field="workflow_id"))
        if status is not None:
            if status.value not in _STATUS_VALUES:
                raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "unknown proposal status")
            clauses.append("status = ?")
            params.append(status.value)
        params.append(limit)
        with self._transaction(conn) as ambient:
            rows = self._all(
                ambient,
                f"SELECT {_PROPOSAL_COLUMNS} FROM workbuddy_improvement_proposals"
                f" WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, proposal_id LIMIT ?",
                params,
            )
            return [_proposal_from_row(row) for row in rows]

    def transition(
        self,
        proposal_id: str,
        *,
        expect_status: ProposalStatus,
        expect_revision: int,
        status: ProposalStatus,
        fields: Mapping[str, Any],
        conn: Any = None,
    ) -> ProposalRecord | None:
        """Compare-and-swap on ``(status, workflow_revision)`` for the tenant row."""
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        unknown = set(fields) - _PROMOTION_FIELDS
        if unknown:
            raise WorkBuddyError(
                ERROR_INVALID_ARGUMENT, f"unknown proposal fields: {sorted(unknown)}"
            )
        assignments = ["status = ?", "updated_at = ?", *(f"{name} = ?" for name in fields)]
        params: list[Any] = [status.value, now_ts(), *fields.values()]
        params.extend((tenant_id, public_id, expect_status.value, int(expect_revision)))
        with self._transaction(conn) as ambient:
            cursor = ambient.execute(
                f"UPDATE workbuddy_improvement_proposals SET {', '.join(assignments)}"
                " WHERE tenant_id = ? AND proposal_id = ? AND status = ? AND workflow_revision = ?"
                f" RETURNING {_PROPOSAL_COLUMNS}",
                params,
            )
            row = cursor.fetchone()
            return _proposal_from_row(row) if row is not None else None

    def active_canary(self, workflow_id: str, *, conn: Any = None) -> ProposalRecord | None:
        """The live canary for a workflow, if candidate traffic is currently routed."""
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(workflow_id, field="workflow_id")
        with self._transaction(conn) as ambient:
            row = self._one(
                ambient,
                f"SELECT {_PROPOSAL_COLUMNS} FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND workflow_id = ? AND status = 'canary' AND canary_stopped_at IS NULL"
                " ORDER BY created_at DESC LIMIT 1",
                (tenant_id, public_id),
            )
            return _proposal_from_row(row) if row is not None else None

    def workflow_pointer(self, workflow_id: str, *, conn: Any = None) -> WorkflowPointer | None:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(workflow_id, field="workflow_id")
        with self._transaction(conn) as ambient:
            row = self._one(
                ambient,
                "SELECT w.workflow_id, w.revision, w.active_version_id, v.definition_sha256"
                " FROM workbuddy_workflows w"
                " LEFT JOIN workbuddy_workflow_versions v"
                " ON v.tenant_id = w.tenant_id AND v.workflow_version_id = w.active_version_id"
                " WHERE w.tenant_id = ? AND w.workflow_id = ?",
                (tenant_id, public_id),
            )
            if row is None:
                return None
            return WorkflowPointer(
                workflow_id=str(row["workflow_id"]),
                revision=_int(row["revision"]),
                active_version_id=(
                    None if row["active_version_id"] is None else str(row["active_version_id"])
                ),
                active_definition_hash=(
                    None if row["definition_sha256"] is None else str(row["definition_sha256"])
                ),
            )

    # ── reviews ──────────────────────────────────────────────────────────────

    def list_reviews(self, proposal_id: str, *, conn: Any = None) -> list[ReviewRecord]:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        with self._transaction(conn) as ambient:
            rows = self._all(
                ambient,
                "SELECT review_id, proposal_id, reviewer_user_id, reviewer_membership_id,"
                " decision, comment, created_at FROM workbuddy_proposal_reviews"
                " WHERE tenant_id = ? AND proposal_id = ? ORDER BY created_at, review_id",
                (tenant_id, public_id),
            )
            return [_review_from_row(row) for row in rows]

    def add_review(self, review: NewReview, *, conn: Any = None) -> ReviewRecord:
        tenant_id = self._require_tenant()
        proposal_id = normalize_uuid(review.proposal_id, field="proposal_id")
        reviewer_user_id = normalize_user_id(review.reviewer_user_id)
        membership_id = self._require_membership(review.reviewer_membership_id)
        if review.decision not in (ReviewDecision.APPROVED, ReviewDecision.REJECTED):
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "decision must be approved or rejected")
        review_id = str(uuid.uuid4())
        created_at = int(review.created_at or now_ts())
        with self._transaction(conn) as ambient:
            try:
                ambient.execute(
                    "INSERT INTO workbuddy_proposal_reviews ("
                    " tenant_id, review_id, proposal_id, reviewer_user_id, reviewer_membership_id,"
                    " decision, comment, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        review_id,
                        proposal_id,
                        reviewer_user_id,
                        membership_id,
                        review.decision.value,
                        review.comment,
                        created_at,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - translated to a typed conflict
                if _is_unique_violation(exc):
                    raise ProposalConflictError(
                        ERROR_DUPLICATE_REVIEW, "this reviewer already voted on the proposal"
                    ) from exc
                raise
            row = self._one(
                ambient,
                "SELECT review_id, proposal_id, reviewer_user_id, reviewer_membership_id,"
                " decision, comment, created_at FROM workbuddy_proposal_reviews"
                " WHERE tenant_id = ? AND review_id = ?",
                (tenant_id, review_id),
            )
            if row is None:  # pragma: no cover - insert just succeeded
                raise ProposalConflictError(ERROR_STATE_CONFLICT, "review insert was not visible")
            return _review_from_row(row)

    # ── shadow evidence ──────────────────────────────────────────────────────

    def list_shadow_runs(self, proposal_id: str, *, conn: Any = None) -> list[ShadowRunRow]:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        with self._transaction(conn) as ambient:
            rows = self._all(
                ambient,
                "SELECT shadow_run_id, settled, replay_only, live_side_effects, evidence_sha256,"
                " created_at FROM workbuddy_proposal_shadow_runs"
                " WHERE tenant_id = ? AND proposal_id = ? ORDER BY created_at, shadow_run_id",
                (tenant_id, public_id),
            )
            return [_shadow_from_row(row) for row in rows]

    def add_shadow_run(
        self, proposal_id: str, run: ShadowRunRow, *, conn: Any = None
    ) -> ShadowRunRow:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        if run.live_side_effects < 0:
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "live_side_effects must not be negative")
        evidence = str(run.evidence_hash).strip().lower()
        if len(evidence) != 64 or any(char not in "0123456789abcdef" for char in evidence):
            raise WorkBuddyError(
                ERROR_INVALID_ARGUMENT, "evidence_hash must be a sha256 hex digest"
            )
        now = now_ts()
        with self._transaction(conn) as ambient:
            proposal = self._one(
                ambient,
                "SELECT candidate_version_id FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND proposal_id = ?",
                (tenant_id, public_id),
            )
            if proposal is None:
                raise ProposalNotFoundError(f"proposal {public_id} is not visible")
            ambient.execute(
                "INSERT INTO workbuddy_proposal_shadow_runs ("
                " tenant_id, shadow_run_id, proposal_id, candidate_version_id, replay_only,"
                " live_side_effects, settled, evidence_sha256, settled_at, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    str(uuid.uuid4()),
                    public_id,
                    str(proposal["candidate_version_id"]),
                    bool(run.replay_only),
                    int(run.live_side_effects),
                    bool(run.settled),
                    now if run.settled else None,
                    now,
                ),
            )
            return run

    # ── canary evaluations ───────────────────────────────────────────────────

    def list_evaluations(self, proposal_id: str, *, conn: Any = None) -> list[EvaluationRow]:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        with self._transaction(conn) as ambient:
            rows = self._all(
                ambient,
                "SELECT evaluation_id, phase, window_start, window_end, baseline_settled_runs,"
                " candidate_settled_runs, baseline_success_rate, candidate_success_rate,"
                " baseline_p95_latency_ms, candidate_p95_latency_ms, baseline_avg_tokens,"
                " candidate_avg_tokens, safety_violations, passed, safety_stop, full_days,"
                " failures, created_at FROM workbuddy_proposal_evaluations"
                " WHERE tenant_id = ? AND proposal_id = ? ORDER BY created_at, evaluation_id",
                (tenant_id, public_id),
            )
            return [_evaluation_from_row(row) for row in rows]

    def add_evaluation(
        self,
        proposal_id: str,
        evaluation: NewEvaluation,
        verdict: GateVerdict,
        *,
        conn: Any = None,
    ) -> EvaluationRow:
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        if evaluation.phase not in ("shadow", "canary"):
            raise WorkBuddyError(ERROR_INVALID_ARGUMENT, "phase must be shadow or canary")
        evaluation_id = str(uuid.uuid4())
        created_at = int(evaluation.created_at or now_ts())
        with self._transaction(conn) as ambient:
            exists = self._one(
                ambient,
                "SELECT 1 AS present FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND proposal_id = ?",
                (tenant_id, public_id),
            )
            if exists is None:
                raise ProposalNotFoundError(f"proposal {public_id} is not visible")
            ambient.execute(
                "INSERT INTO workbuddy_proposal_evaluations ("
                " tenant_id, evaluation_id, proposal_id, phase, window_start, window_end,"
                " baseline_settled_runs, candidate_settled_runs, baseline_success_rate,"
                " candidate_success_rate, baseline_p95_latency_ms, candidate_p95_latency_ms,"
                " baseline_avg_tokens, candidate_avg_tokens, safety_violations, passed,"
                " safety_stop, full_days, failures, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    evaluation_id,
                    public_id,
                    evaluation.phase,
                    int(evaluation.window_start),
                    int(evaluation.window_end),
                    int(evaluation.baseline.settled_runs),
                    int(evaluation.candidate.settled_runs),
                    float(evaluation.baseline.success_rate),
                    float(evaluation.candidate.success_rate),
                    float(evaluation.baseline.p95_latency_ms),
                    float(evaluation.candidate.p95_latency_ms),
                    float(evaluation.baseline.avg_tokens),
                    float(evaluation.candidate.avg_tokens),
                    int(evaluation.candidate.safety_violations),
                    bool(verdict.passed),
                    bool(verdict.safety_stop),
                    int(verdict.full_days),
                    _dump(list(verdict.failures)),
                    created_at,
                ),
            )
            row = self._one(
                ambient,
                "SELECT evaluation_id, phase, window_start, window_end, baseline_settled_runs,"
                " candidate_settled_runs, baseline_success_rate, candidate_success_rate,"
                " baseline_p95_latency_ms, candidate_p95_latency_ms, baseline_avg_tokens,"
                " candidate_avg_tokens, safety_violations, passed, safety_stop, full_days,"
                " failures, created_at FROM workbuddy_proposal_evaluations"
                " WHERE tenant_id = ? AND evaluation_id = ?",
                (tenant_id, evaluation_id),
            )
            if row is None:  # pragma: no cover - insert just succeeded
                raise ProposalConflictError(
                    ERROR_STATE_CONFLICT, "evaluation insert was not visible"
                )
            return _evaluation_from_row(row)

    # ── promotion ────────────────────────────────────────────────────────────

    def apply_promotion(
        self,
        proposal_id: str,
        *,
        expect_revision: int,
        actor_user_id: int,
        conn: Any = None,
    ) -> ProposalRecord | None:
        """Publish the candidate as a new promoted version and move the pointer.

        The proposal row and the workflow row are both locked, so two promoters
        (or a promoter and an external ``activate``) serialise on
        ``workbuddy_workflows.revision``; the loser returns ``None`` and the
        caller reports a stale revision.
        """
        tenant_id = self._require_tenant()
        public_id = normalize_uuid(proposal_id, field="proposal_id")
        expected_revision = int(expect_revision)
        now = now_ts()
        with self._transaction(conn) as ambient:
            proposal = self._one(
                ambient,
                f"SELECT {_PROPOSAL_COLUMNS} FROM workbuddy_improvement_proposals"
                " WHERE tenant_id = ? AND proposal_id = ? FOR UPDATE",
                (tenant_id, public_id),
            )
            if proposal is None:
                return None
            record = _proposal_from_row(proposal)
            if record.status is not ProposalStatus.CANARY:
                return None
            if record.workflow_revision != expected_revision:
                return None
            workflow = self._one(
                ambient,
                "SELECT revision, status, active_version_id FROM workbuddy_workflows"
                " WHERE tenant_id = ? AND workflow_id = ? FOR UPDATE",
                (tenant_id, record.workflow_id),
            )
            if workflow is None or str(workflow["status"]) != "active":
                return None
            if _int(workflow["revision"]) != expected_revision:
                return None
            if str(workflow["active_version_id"] or "") != record.base_version_id:
                return None
            candidate = self._one(
                ambient,
                "SELECT definition, definition_sha256 FROM workbuddy_workflow_versions"
                " WHERE tenant_id = ? AND workflow_id = ? AND workflow_version_id = ?",
                (tenant_id, record.workflow_id, record.candidate_version_id),
            )
            if candidate is None:
                raise WorkBuddyError(ERROR_VERSION_IMMUTABLE, "the candidate version is missing")
            promoted_version_id = str(uuid.uuid4())
            version_number = self._next_version_number(ambient, tenant_id, record.workflow_id)
            self._insert_version(
                ambient,
                tenant_id=tenant_id,
                workflow_id=record.workflow_id,
                version_id=promoted_version_id,
                version_number=version_number,
                definition=_as_json(candidate["definition"]),
                content_hash=str(candidate["definition_sha256"]),
                origin="promotion",
                base_version_id=record.base_version_id,
                source_version_id=record.candidate_version_id,
                change_summary=f"promoted proposal {record.proposal_id}",
                # The promoted version is created by the promoter, not the author.
                created_by=actor_user_id,
                created_by_membership_id=None,
                created_at=now,
            )
            moved = ambient.execute(
                "UPDATE workbuddy_workflows SET active_version_id = ?, shadow_version_id = NULL,"
                " revision = revision + 1, updated_at = now()"
                " WHERE tenant_id = ? AND workflow_id = ? AND revision = ? AND active_version_id = ?",
                (
                    promoted_version_id,
                    tenant_id,
                    record.workflow_id,
                    expected_revision,
                    record.base_version_id,
                ),
            )
            if int(getattr(moved, "rowcount", 0) or 0) != 1:
                raise ProposalConflictError(
                    ERROR_BASE_CONFLICT, "the workflow revision changed during promotion"
                )
            cursor = ambient.execute(
                "UPDATE workbuddy_improvement_proposals SET status = 'applied',"
                " applied_version_id = ?, applied_at = ?, canary_stopped_at = coalesce(canary_stopped_at, ?),"
                " canary_stop_reason = coalesce(canary_stop_reason, 'applied'), updated_at = ?"
                " WHERE tenant_id = ? AND proposal_id = ? AND status = 'canary'"
                f" RETURNING {_PROPOSAL_COLUMNS}",
                (promoted_version_id, now, now, now, tenant_id, public_id),
            )
            updated = cursor.fetchone()
            if updated is None:
                return None
            superseded = ambient.execute(
                "UPDATE workbuddy_improvement_proposals SET status = 'superseded',"
                " status_reason = 'superseded', canary_stopped_at = coalesce(canary_stopped_at, ?),"
                " canary_stop_reason = coalesce(canary_stop_reason, 'superseded'), updated_at = ?"
                " WHERE tenant_id = ? AND workflow_id = ? AND proposal_id <> ?"
                " AND status IN ('under_review','approved','shadow','canary')",
                (now, now, tenant_id, record.workflow_id, public_id),
            )
            if int(getattr(superseded, "rowcount", 0) or 0) < 0:  # pragma: no cover - defensive
                raise ProposalConflictError(ERROR_STATE_CONFLICT, "supersede failed")
            return _proposal_from_row(updated)

    # ── workflow table integration ───────────────────────────────────────────

    def _next_version_number(self, conn: Any, tenant_id: str, workflow_id: str) -> int:
        row = self._one(
            conn,
            "SELECT coalesce(max(version_number), 0) + 1 AS next FROM workbuddy_workflow_versions"
            " WHERE tenant_id = ? AND workflow_id = ?",
            (tenant_id, workflow_id),
        )
        return _int(row["next"]) if row is not None else 1

    def _insert_version(
        self,
        conn: Any,
        *,
        tenant_id: str,
        workflow_id: str,
        version_id: str,
        version_number: int,
        definition: Mapping[str, Any],
        content_hash: str,
        origin: str,
        base_version_id: str,
        source_version_id: str,
        change_summary: str,
        created_by: int | None,
        created_by_membership_id: str | None,
        created_at: int,
    ) -> None:
        """Insert an immutable workflow version row (candidate or promoted)."""
        conn.execute(
            "INSERT INTO workbuddy_workflow_versions ("
            " tenant_id, workflow_version_id, workflow_id, version_number, definition,"
            " definition_sha256, origin, base_version_id, source_version_id, change_summary,"
            " created_by, created_by_membership_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, to_timestamp(?))",
            (
                tenant_id,
                version_id,
                workflow_id,
                version_number,
                _dump(definition),
                content_hash,
                origin,
                base_version_id,
                source_version_id,
                change_summary,
                created_by,
                created_by_membership_id,
                created_at,
            ),
        )


def _proposal_from_row(row: DbRow) -> ProposalRecord:
    changes = _as_json(row["semantic_changes"]) or []
    status = ProposalStatus(str(row["status"]))
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        workflow_id=str(row["workflow_id"]),
        workflow_revision=_int(row["workflow_revision"]),
        base_version_id=str(row["base_version_id"]),
        base_content_hash=str(row["base_content_hash"]),
        candidate_version_id=str(row["candidate_version_id"]),
        candidate_content_hash=str(row["candidate_content_hash"]),
        status=status,
        risk_level=str(row["risk_level"]),  # type: ignore[arg-type]
        pii_involved=bool(row["pii_involved"]),
        required_approvals=_int(row["required_approvals"]),
        requires_manual_shadow=bool(row["requires_manual_shadow"]),
        change_summary=str(row["change_summary"]),
        changes=tuple(
            SemanticChange(
                path=str(change.get("path", "")),
                kind=str(change.get("kind", "replaced")),  # type: ignore[arg-type]
                old=change.get("old"),
                new=change.get("new"),
            )
            for change in changes
            if isinstance(change, Mapping)
        ),
        created_by_user_id=_int(row["created_by_user_id"]),
        created_by_membership_id=str(row["created_by_membership_id"]),
        created_at=_int(row["created_at"]),
        updated_at=_int(row["updated_at"]),
        canary_ratio_bp=_optional_int(row["canary_ratio_bp"]),
        canary_started_at=_optional_int(row["canary_started_at"]),
        canary_stopped_at=_optional_int(row["canary_stopped_at"]),
        canary_stop_reason=None
        if row["canary_stop_reason"] is None
        else str(row["canary_stop_reason"]),
        applied_version_id=None
        if row["applied_version_id"] is None
        else str(row["applied_version_id"]),
        status_reason=None if row["status_reason"] is None else str(row["status_reason"]),
    )


def _review_from_row(row: DbRow) -> ReviewRecord:
    return ReviewRecord(
        review_id=str(row["review_id"]),
        proposal_id=str(row["proposal_id"]),
        reviewer_user_id=_int(row["reviewer_user_id"]),
        reviewer_membership_id=(
            None if row["reviewer_membership_id"] is None else str(row["reviewer_membership_id"])
        ),
        decision=ReviewDecision(str(row["decision"])),
        comment=str(row["comment"]),
        created_at=_int(row["created_at"]),
    )


def _shadow_from_row(row: DbRow) -> ShadowRunRow:
    return ShadowRunRow(
        run_id=str(row["shadow_run_id"]),
        settled=bool(row["settled"]),
        replay_only=bool(row["replay_only"]),
        live_side_effects=_int(row["live_side_effects"]),
        evidence_hash=str(row["evidence_sha256"]),
        created_at=_int(row["created_at"]),
    )


def _evaluation_from_row(row: DbRow) -> EvaluationRow:
    baseline = PhaseMetrics(
        settled_runs=_int(row["baseline_settled_runs"]),
        success_rate=float(row["baseline_success_rate"]),
        p95_latency_ms=float(row["baseline_p95_latency_ms"]),
        avg_tokens=float(row["baseline_avg_tokens"]),
        safety_violations=0,
    )
    candidate = PhaseMetrics(
        settled_runs=_int(row["candidate_settled_runs"]),
        success_rate=float(row["candidate_success_rate"]),
        p95_latency_ms=float(row["candidate_p95_latency_ms"]),
        avg_tokens=float(row["candidate_avg_tokens"]),
        safety_violations=_int(row["safety_violations"]),
    )
    failures = _as_json(row["failures"]) or []
    return EvaluationRow(
        evaluation_id=str(row["evaluation_id"]),
        phase=str(row["phase"]),  # type: ignore[arg-type]
        window_start=_int(row["window_start"]),
        window_end=_int(row["window_end"]),
        baseline=baseline,
        candidate=candidate,
        verdict=GateVerdict(
            passed=bool(row["passed"]),
            failures=tuple(str(failure) for failure in failures),
            safety_stop=bool(row["safety_stop"]),
            full_days=_int(row["full_days"]),
        ),
        created_at=_int(row["created_at"]),
    )

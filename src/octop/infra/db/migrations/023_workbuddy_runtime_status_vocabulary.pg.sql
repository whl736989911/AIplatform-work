-- Schema v23: align the runtime status vocabulary with the published contract.
--
-- Migration 018 was written with an invented vocabulary (`pending`, `succeeded`,
-- `cancelled`) and without `waiting_reconciliation`, so an execution that is
-- waiting on an unknown external write had no state to occupy. The published
-- contract fixes the sets as
--
--   execution: queued, running, waiting_approval, waiting_reconciliation,
--              success, failed, partial, canceled
--   step run : queued, running, waiting_approval, waiting_reconciliation,
--              success, failed, skipped, canceled
--
-- Nothing has shipped against 018, so the correction is applied in place; the
-- rows that existed under the old vocabulary are mapped first.

UPDATE workbuddy_executions SET status = 'queued'   WHERE status = 'pending'; -- legacy vocabulary
UPDATE workbuddy_executions SET status = 'success'  WHERE status = 'succeeded'; -- legacy vocabulary
UPDATE workbuddy_executions SET status = 'canceled' WHERE status = 'cancelled'; -- legacy vocabulary

UPDATE workbuddy_step_runs SET status = 'success'  WHERE status = 'succeeded'; -- legacy vocabulary
UPDATE workbuddy_step_runs SET status = 'canceled' WHERE status = 'cancelled'; -- legacy vocabulary

ALTER TABLE workbuddy_executions DROP CONSTRAINT IF EXISTS workbuddy_executions_status_check;
ALTER TABLE workbuddy_executions ADD CONSTRAINT workbuddy_executions_status_check
  CHECK (status IN ('queued', 'running', 'waiting_approval', 'waiting_reconciliation',
                    'success', 'failed', 'partial', 'canceled'));

ALTER TABLE workbuddy_step_runs DROP CONSTRAINT IF EXISTS workbuddy_step_runs_status_check;
ALTER TABLE workbuddy_step_runs ADD CONSTRAINT workbuddy_step_runs_status_check
  CHECK (status IN ('queued', 'running', 'waiting_approval', 'waiting_reconciliation',
                    'success', 'failed', 'skipped', 'canceled'));

-- The partial index over open executions has to name the new queued state.
DROP INDEX IF EXISTS idx_wb_executions_open;
CREATE INDEX idx_wb_executions_open ON workbuddy_executions (tenant_id, status)
  WHERE status IN ('queued', 'running', 'waiting_approval', 'waiting_reconciliation');

-- Approval decisions use the same words at rest as in the API body.
UPDATE workbuddy_approval_requests SET decision = 'approved' WHERE decision = 'approve';
UPDATE workbuddy_approval_requests SET decision = 'rejected' WHERE decision = 'reject';
ALTER TABLE workbuddy_approval_requests DROP CONSTRAINT IF EXISTS workbuddy_approval_requests_decision_check;
ALTER TABLE workbuddy_approval_requests ADD CONSTRAINT workbuddy_approval_requests_decision_check
  CHECK (decision IN ('approved', 'rejected'));

-- Reconciliations carry one final decision per step run: the evidence the
-- operator submitted (by reference, not inline), and for a confirmed success the
-- payload holding the verified result of the original call. An undecidable
-- outcome is not a row at all -- the step simply keeps waiting.
ALTER TABLE workbuddy_reconciliations RENAME COLUMN status TO decision;
ALTER TABLE workbuddy_reconciliations RENAME COLUMN recorded_by_user_id TO decided_by_user_id;
ALTER TABLE workbuddy_reconciliations RENAME COLUMN evidence_sha256 TO evidence_hash;
ALTER TABLE workbuddy_reconciliations RENAME COLUMN external_ref TO external_request_id;
ALTER TABLE workbuddy_reconciliations ADD COLUMN step_run_id uuid;
ALTER TABLE workbuddy_reconciliations ADD COLUMN evidence_ref uuid;
ALTER TABLE workbuddy_reconciliations ADD COLUMN result_payload_ref uuid;
ALTER TABLE workbuddy_reconciliations ADD COLUMN note text;

-- Pre-release rows cannot satisfy the contract (they carry no step run and no
-- evidence reference), so they are dropped rather than migrated.
DELETE FROM workbuddy_reconciliations;
ALTER TABLE workbuddy_reconciliations DROP COLUMN IF EXISTS evidence;
ALTER TABLE workbuddy_reconciliations DROP COLUMN IF EXISTS resolved_at;
-- One decision per step run; the node is discovered through that step.
ALTER TABLE workbuddy_reconciliations DROP COLUMN IF EXISTS node_id;

ALTER TABLE workbuddy_reconciliations
  ALTER COLUMN step_run_id SET NOT NULL,
  ALTER COLUMN evidence_ref SET NOT NULL,
  ALTER COLUMN note SET NOT NULL;

ALTER TABLE workbuddy_reconciliations DROP CONSTRAINT IF EXISTS workbuddy_reconciliations_status_check;
ALTER TABLE workbuddy_reconciliations
  ADD CONSTRAINT workbuddy_reconciliations_decision_check
    CHECK (decision IN ('confirmed_success', 'confirmed_failed')),
  ADD CONSTRAINT workbuddy_reconciliations_result_check
    CHECK ((decision = 'confirmed_success') = (result_payload_ref IS NOT NULL)),
  ADD CONSTRAINT workbuddy_reconciliations_step_unique UNIQUE (tenant_id, step_run_id),
  ADD CONSTRAINT workbuddy_reconciliations_step_run_fkey
    FOREIGN KEY (tenant_id, step_run_id) REFERENCES workbuddy_step_runs (tenant_id, id)
    ON DELETE CASCADE,
  ADD CONSTRAINT workbuddy_reconciliations_evidence_fkey
    FOREIGN KEY (tenant_id, evidence_ref) REFERENCES workbuddy_execution_payloads (tenant_id, id),
  ADD CONSTRAINT workbuddy_reconciliations_result_fkey
    FOREIGN KEY (tenant_id, result_payload_ref)
    REFERENCES workbuddy_execution_payloads (tenant_id, id);

UPDATE _schema_version SET version = 23;

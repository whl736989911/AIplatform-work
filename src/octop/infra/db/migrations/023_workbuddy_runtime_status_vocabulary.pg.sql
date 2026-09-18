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

UPDATE _schema_version SET version = 23;

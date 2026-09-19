-- Schema v28: the dispatch intent of an external call (PostgreSQL only).
--
-- The contract requires the intent to be durable *before* the call leaves the
-- process: a worker that dies between dispatch and answer must leave evidence of
-- what it was about to do, so the reconciliation path can name the operation
-- instead of guessing whether a write happened.
--
--   dispatch_intent_at  when the attempt decided to call
--   tool_id             the declared tool of the node (a deployment resolves its
--                       published revision from this)
--   tool_call_key       the stable logical operation key: execution_id:step_id,
--                       reused by every attempt of the same node, which is what
--                       an idempotent provider de-duplicates on
--
-- The three travel together: a call key without the moment it was decided would
-- say nothing about whether the call had left the process yet.

ALTER TABLE workbuddy_step_runs ADD COLUMN IF NOT EXISTS tool_id text;
ALTER TABLE workbuddy_step_runs ADD COLUMN IF NOT EXISTS tool_call_key text;
ALTER TABLE workbuddy_step_runs ADD COLUMN IF NOT EXISTS dispatch_intent_at timestamptz;

ALTER TABLE workbuddy_step_runs
  DROP CONSTRAINT IF EXISTS workbuddy_step_runs_dispatch_intent_check;
ALTER TABLE workbuddy_step_runs
  ADD CONSTRAINT workbuddy_step_runs_dispatch_intent_check
    CHECK ((tool_call_key IS NULL) = (dispatch_intent_at IS NULL));

-- Reconciliation looks a parked call up by its logical key.
CREATE INDEX IF NOT EXISTS idx_wb_step_runs_call_key
  ON workbuddy_step_runs (tenant_id, tool_call_key)
  WHERE tool_call_key IS NOT NULL;

UPDATE _schema_version SET version = 28;

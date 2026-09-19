-- Schema v24: an execution records how it was routed through a canary.
--
-- Contract 5.2.3 requires every execution to keep the routing facts it was
-- created with: the proposal and cohort it belonged to, the deterministic bucket
-- that decided the cohort, the ratio in force at that moment, and the subject the
-- bucket was computed from. Reviewing a canary after the fact is only possible
-- when those are stored per execution rather than recomputed later from whatever
-- the proposal now says.

ALTER TABLE workbuddy_executions ADD COLUMN proposal_id uuid;
ALTER TABLE workbuddy_executions ADD COLUMN cohort text;
ALTER TABLE workbuddy_executions ADD COLUMN bucket integer;
ALTER TABLE workbuddy_executions ADD COLUMN route_canary_percent integer;
ALTER TABLE workbuddy_executions ADD COLUMN subject text;

ALTER TABLE workbuddy_executions
  ADD CONSTRAINT workbuddy_executions_cohort_check
    CHECK (cohort IS NULL OR cohort IN ('production', 'canary', 'baseline')),
  ADD CONSTRAINT workbuddy_executions_bucket_check
    CHECK (bucket IS NULL OR (bucket >= 0 AND bucket < 10000)),
  ADD CONSTRAINT workbuddy_executions_route_percent_check
    CHECK (
      route_canary_percent IS NULL
      OR (route_canary_percent >= 0 AND route_canary_percent <= 10000)
    ),
  -- An evaluation execution always names its proposal; a production run never does.
  ADD CONSTRAINT workbuddy_executions_proposal_fkey
    FOREIGN KEY (tenant_id, proposal_id)
    REFERENCES workbuddy_improvement_proposals (tenant_id, proposal_id);

CREATE INDEX idx_wb_executions_proposal ON workbuddy_executions (tenant_id, proposal_id, cohort)
  WHERE proposal_id IS NOT NULL;

UPDATE _schema_version SET version = 24;

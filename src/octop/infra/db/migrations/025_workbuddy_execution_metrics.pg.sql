-- Schema v25: an execution keeps the active time it actually spent running.
--
-- Contract 5.2.3 compares p95 *active* latency between the baseline and the
-- candidate cohort, and states that approval and reconciliation waits are
-- reported separately because they would otherwise hide inside a shorter active
-- average. Wall-clock start/finish therefore cannot serve as the metric: it
-- includes the waits. The engine already measures every step it runs, so the
-- execution stores their sum.

ALTER TABLE workbuddy_executions
  ADD COLUMN active_duration_ms bigint NOT NULL DEFAULT 0
    CHECK (active_duration_ms >= 0),
  -- Tokens the model adapters reported for this execution. The contract compares
  -- average token cost between cohorts, and a deployment that reports none is
  -- not comparable, which the gates treat as a failure rather than a pass.
  ADD COLUMN token_usage bigint NOT NULL DEFAULT 0
    CHECK (token_usage >= 0);

UPDATE _schema_version SET version = 25;

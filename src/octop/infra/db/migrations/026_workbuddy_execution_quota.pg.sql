-- Schema v26: the execution quota metrics the contract's model names.
--
-- Contract 5.4 gives the tenant quota model two execution metrics: a monthly
-- execution allowance and a concurrency ceiling, and 5.1.5 says a run that
-- cannot start yet waits for a slot instead of exceeding the total. The runtime
-- already reserves one `executions` unit per accepted run and records the usage
-- when the run settles, so the only thing missing was a metric to enforce
-- against: without a row here the limits lookup found nothing and the ceiling
-- was never applied.
--
-- `ON CONFLICT DO NOTHING` keeps the migration idempotent and leaves any limit
-- an operator has already tuned in place.

INSERT INTO workbuddy_quota_metrics(metric, unit, default_limit, hard_cap, sort_order) VALUES
  ('executions', 'executions', 10000, 1000000, 70),
  ('concurrency', 'executions', 8, 64, 80)
ON CONFLICT (metric) DO NOTHING;

UPDATE _schema_version SET version = 26;

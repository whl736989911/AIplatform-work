-- Schema v32: a step records the input it was handed and the model usage its
-- call reported (PostgreSQL only).
--
-- Contract §4.6 (execution detail) requires a run to be debuggable node by node:
-- what each node was given, what it produced, how long it took and what it cost.
-- The engine already knew all of it -- 018 declared the ``step_input`` payload
-- kind and a step's ``input_sha256`` for exactly this purpose -- but neither had
-- a reader, so an operator could see a failed node's error and nothing about the
-- call that produced it.
--
-- What is added here is *recording and reading*, never behaviour:
--
--   input_payload_id  the activation the attempt was dispatched with, kept in
--                     the execution's append-only payload ledger (kind
--                     'step_input') rather than copied onto the hot step table,
--                     which every replay and detail read scans.
--   input_sha256      the digest of that same input (018), so the fact survives
--                     even when the payload is too large for the ledger's
--                     per-payload ceiling and only its fingerprint is kept.
--   token_usage       the usage object the model adapter reported for this
--                     call, in the shape the adapter sent it. It stays NULL for
--                     a node that called no model, and no counter is ever
--                     synthesised: a deployment whose adapter reports nothing
--                     stores nothing, which the cohort gates report as a
--                     failure rather than a silent zero.
--
-- The execution-level facts the same detail needs (inputs, outputs,
-- active_duration_ms, token_usage) are already stored by 018 and 025.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * each top-level statement ends its final line with a semicolon, and no
--     statement may contain a semicolon directly followed by a newline;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file.
-- Every statement is idempotent so a half-applied run can simply be repeated.

ALTER TABLE workbuddy_step_runs ADD COLUMN IF NOT EXISTS input_payload_id uuid;
ALTER TABLE workbuddy_step_runs ADD COLUMN IF NOT EXISTS token_usage jsonb;

-- The input belongs to the tenant that owns the payload, exactly as the
-- reconciliation references do (023): a step can never point at another
-- tenant's payload, and a referenced input can never be deleted out from under
-- the step that used it.
ALTER TABLE workbuddy_step_runs
  DROP CONSTRAINT IF EXISTS workbuddy_step_runs_input_payload_fkey;
ALTER TABLE workbuddy_step_runs
  ADD CONSTRAINT workbuddy_step_runs_input_payload_fkey
    FOREIGN KEY (tenant_id, input_payload_id)
    REFERENCES workbuddy_execution_payloads (tenant_id, id);

-- Usage is an object or nothing: a bare number would lose the adapter's own
-- accounting, and any other shape is a bug in the recording path, not a fact.
ALTER TABLE workbuddy_step_runs
  DROP CONSTRAINT IF EXISTS workbuddy_step_runs_token_usage_check;
ALTER TABLE workbuddy_step_runs
  ADD CONSTRAINT workbuddy_step_runs_token_usage_check
    CHECK (token_usage IS NULL OR jsonb_typeof(token_usage) = 'object');

UPDATE _schema_version SET version = 32;

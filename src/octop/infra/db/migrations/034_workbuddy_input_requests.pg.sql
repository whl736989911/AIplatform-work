-- Schema v34: a running execution can ask a person for input and wait for the
-- answer (PostgreSQL only).
--
-- Until now the only pause the runtime understood was an approval: a step
-- either decided by itself or handed the fixed proposal 018 stores to a
-- reviewer. A step that is missing a fact only a person has -- an invoice
-- number, one of two accounts to post against -- had no way to ask for it, so
-- the only way to supply it was to cancel the run and start over with different
-- inputs, which throws away the work already done and the position in the
-- graph. This version adds the second pause: the run stays alive, holds its
-- lease fence and waits for a person to answer.
--
-- The pair is deliberately shaped like the approval pair of 018
-- (workbuddy_approval_requests / workbuddy_approval_candidates) so the runtime
-- keeps one waiting pattern instead of two:
--
--   workbuddy_input_requests   the question one node asks: the prompt, the form
--                              the answer has to fill, the digest of that form
--                              (a late answer can then be proven to belong to
--                              the form that was actually shown), the workflow
--                              version the step was locked to, and the values
--                              that came back.
--   workbuddy_input_assignees  who may answer, and what each of them did.
--
-- The status vocabulary gains ``waiting_input`` on the execution and on the
-- step run: the run is alive, it is not waiting on a decision about a proposal
-- and not on an external write. Nothing else in either set moves -- an
-- execution still ends ``partial`` and a step can still be ``skipped``.
--
-- Invariants enforced below:
--   * UUID identifiers and compound (tenant_id, parent_id) foreign keys, so a
--     row can never point at another tenant's parent.
--   * one question per (execution, node): a node that is entered again finds
--     the question it already asked instead of opening a second one.
--   * ENABLE + FORCE ROW LEVEL SECURITY and a FOR ALL policy on both tables, so
--     the tenant predicate holds on reads and on writes alike.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * each statement ends with its semicolon and no statement contains a
--     semicolon directly followed by a newline;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file;
--   * every statement is idempotent so a half-applied run can simply be
--     repeated.

-- ── Status vocabulary: the run is alive and waiting for a person ─────────────

ALTER TABLE workbuddy_executions DROP CONSTRAINT IF EXISTS workbuddy_executions_status_check;
ALTER TABLE workbuddy_executions ADD CONSTRAINT workbuddy_executions_status_check
  CHECK (status IN ('queued', 'running', 'waiting_approval', 'waiting_input',
                    'waiting_reconciliation', 'success', 'failed', 'partial', 'canceled'));

ALTER TABLE workbuddy_step_runs DROP CONSTRAINT IF EXISTS workbuddy_step_runs_status_check;
ALTER TABLE workbuddy_step_runs ADD CONSTRAINT workbuddy_step_runs_status_check
  CHECK (status IN ('queued', 'running', 'waiting_approval', 'waiting_input',
                    'waiting_reconciliation', 'success', 'failed', 'skipped', 'canceled'));

-- The partial index over an execution that is open but not running has to cover
-- the new state as well: a form-parked run is precisely the kind of open
-- execution this index exists for, and a predicate that omits it would quietly
-- drop those rows out of the plan it was built to serve.
DROP INDEX IF EXISTS idx_wb_executions_open;
CREATE INDEX IF NOT EXISTS idx_wb_executions_open ON workbuddy_executions (tenant_id, status)
  WHERE status IN ('queued', 'running', 'waiting_approval', 'waiting_input',
                   'waiting_reconciliation');

-- The step-run node-type vocabulary is the storage mirror of the compiler's
-- node set, and it had fallen behind it: 018 wrote the five types that existed
-- then, so the explicit input / knowledge / output nodes (A-07) and this
-- version's ask node had no way to record a step. The whole attempt failed on
-- the first step it wrote, with a check violation rather than a workflow error.
-- Keeping the list here in step with NODE_TYPES is the invariant; a node type
-- the compiler accepts must always be storable.
ALTER TABLE workbuddy_step_runs DROP CONSTRAINT IF EXISTS workbuddy_step_runs_node_type_check;
ALTER TABLE workbuddy_step_runs ADD CONSTRAINT workbuddy_step_runs_node_type_check
  CHECK (node_type IN ('tool', 'llm', 'condition', 'approval', 'ask', 'transform',
                       'input', 'knowledge', 'output'));

-- ── The question one node asks ──────────────────────────────────────────────

-- ``values`` and ``values_sha256`` stay NULL until an answer is in, so an open
-- question cannot be mistaken for an answered one by reading the row alone.
-- The form and its digest are written when the question opens, because the form
-- is what the submitted values are checked against.
CREATE TABLE IF NOT EXISTS workbuddy_input_requests (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  node_id text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'submitted', 'expired', 'invalidated')),
  prompt text NOT NULL,
  form jsonb NOT NULL,
  form_sha256 text NOT NULL,
  values jsonb,
  values_sha256 text,
  locked_workflow_version_id uuid NOT NULL,
  locked_workflow_version_hash text NOT NULL,
  expires_at timestamptz,
  submitted_by_user_id integer,
  submitted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, execution_id, node_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE
);

-- The answering queue lists open questions oldest first and nothing else, so
-- the index covers the open work instead of the whole history.
CREATE INDEX IF NOT EXISTS idx_wb_input_requests_open ON workbuddy_input_requests (tenant_id, created_at DESC)
  WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_wb_input_requests_execution ON workbuddy_input_requests (tenant_id, execution_id);

-- ── Who may answer, and who did ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workbuddy_input_assignees (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  input_request_id uuid NOT NULL,
  user_id integer NOT NULL,
  department_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'answered', 'abstained', 'invalidated')),
  submitted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, input_request_id, user_id),
  FOREIGN KEY (tenant_id, input_request_id) REFERENCES workbuddy_input_requests (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wb_input_assignees_user ON workbuddy_input_assignees (tenant_id, user_id, status, created_at DESC);

-- ── Row level security: one tenant per row ──────────────────────────────────

ALTER TABLE workbuddy_input_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_input_requests FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_input_requests_tenant ON workbuddy_input_requests;
CREATE POLICY workbuddy_input_requests_tenant ON workbuddy_input_requests FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_input_assignees ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_input_assignees FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_input_assignees_tenant ON workbuddy_input_assignees;
CREATE POLICY workbuddy_input_assignees_tenant ON workbuddy_input_assignees FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_input_requests FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_input_assignees FROM PUBLIC;

UPDATE _schema_version SET version = 34;

-- Schema v37: what a person changed a workflow's output into -- corrections and
-- supplied facts -- is captured as one stream of structured records
-- (PostgreSQL only).
--
-- Versions 034 and 035 each added the place where a person acts: 034's input
-- request holds the answer to a mid-run question, 035's output review holds the
-- verdict on a settled run. Both store what the person said, and both store it
-- in the shape of the thing that was asked about -- an answer inside the form it
-- filled in, a replacement inside the snapshot it reviewed. That is the right
-- shape for the person doing the work and the wrong shape for anyone trying to
-- learn from it: to answer "which values do people change in this workflow, and
-- what do they change them into" an analyzer had to walk every review's
-- ``corrected`` object and every request's ``values`` blob, and match each entry
-- back to a node and an output key that neither table records. The improvement
-- loop -- A-13 attributing a correction to the step that produced it, A-14
-- turning a run of corrections into a proposal -- had nothing to read.
--
-- This version adds the missing ledger:
--
--   workbuddy_execution_feedback   one row per person-action that replaced or
--                                  completed one of the workflow's own outputs:
--                                  the node and the output key it is about, the
--                                  value before it (NULL when the value it
--                                  replaced was itself JSON null, or when the
--                                  person supplied a fact that was missing
--                                  rather than replacing one), the value after
--                                  it, and the request or the review the action
--                                  came from.
--
-- Nothing in the runtime reads this table to decide anything: it is a record,
-- not a state machine. A row never changes the execution's status, never touches
-- the payload rows and never rewrites ``outputs``; it is written alongside the
-- review or the answer that caused it, and it is the raw material the
-- improvement loop consumes.
--
-- Two closed vocabularies, so an analyzer counts every improvement signal
-- without a fallback branch. ``kind`` is what the person did: ``correction``
-- (the workflow produced a value and a person replaced it) or ``supplied_fact``
-- (the workflow could not know a value and a person provided it). ``source`` is
-- where it came from: ``output_review`` (035) or ``ask`` (034).
--
-- Invariants enforced below:
--   * UUID identifiers and a compound (tenant_id, execution_id) foreign key, so
--     a row can never point at another tenant's execution.
--   * a correction names the output key it corrects, so a correction can be
--     replayed against the workflow that produced the value instead of guessed
--     at. It does not require ``before``: the value it replaced can itself have
--     been JSON null, and a shape rule that demanded a non-null ``before`` would
--     read that correction as no correction at all and refuse the write.
--   * the workflow version is recorded by id and by hash, so a correction made
--     against one version of a workflow is never read back as evidence about
--     another.
--   * ``source_id`` is a plain uuid and not a foreign key on purpose: it points
--     into whichever of the two source tables ``source`` names, and a source row
--     that is later pruned must not erase the correction that came out of it.
--   * ENABLE + FORCE ROW LEVEL SECURITY and a FOR ALL policy, so the tenant
--     predicate holds on reads and on writes alike.
--
-- Maintenance rules for this file (see migrate.py::_split_pg_sql):
--   * each statement ends with its semicolon and no statement contains a
--     semicolon directly followed by a newline;
--   * the connection proxy rewrites every literal question mark into a psycopg
--     placeholder, so question marks must not appear anywhere in this file;
--   * every statement is idempotent so a half-applied run can simply be
--     repeated.

-- ── One person-action against one of the workflow's outputs ─────────────────

-- ``node_id`` and ``output_key`` are nullable because the two kinds do not
-- always have both. A correction always names the key it corrects (the shape
-- constraint below says so) and may still leave ``before`` NULL: the value it
-- replaced can itself have been JSON null, which is a legal output and a
-- different statement from "there was no earlier value". Which of the two a
-- NULL ``before`` means is told apart by ``kind``, not by the column.
-- A supplied fact has neither -- the fact a person hands over can be about the
-- run as a whole rather than about one node's output, and it replaced nothing.
--
-- ``before`` and ``after`` are both jsonb so the correction is the value itself,
-- not a rendering of it, and ``after`` is the one column that can never be NULL:
-- a record of a person's action with no value in it would be a claim that
-- something changed, with nothing to say what. ``before`` stays nullable for the
-- reason above -- a correction of a value that was JSON null has no non-null old
-- value to write -- so the shape constraint below asks for the key, not for the
-- old value.
--
-- ``created_by_user_id`` is a plain integer and not a foreign key, like the
-- actor columns of 034 and 035: the record has to survive a person leaving the
-- tenant, and the actor is provenance rather than ownership.
CREATE TABLE IF NOT EXISTS workbuddy_execution_feedback (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  workflow_id uuid NOT NULL,
  workflow_version_id uuid NOT NULL,
  workflow_version_hash text NOT NULL,
  node_id text,
  output_key text,
  kind text NOT NULL CHECK (kind IN ('correction', 'supplied_fact')),
  source text NOT NULL CHECK (source IN ('output_review', 'ask')),
  source_id uuid,
  before jsonb,
  after jsonb NOT NULL CHECK (jsonb_typeof(after) IS NOT NULL),
  created_by_user_id integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE,
  CHECK (kind <> 'correction' OR output_key IS NOT NULL)
);

-- The attribution pass starts from one workflow and walks its history newest
-- first, so the index carries created_at in that order.
CREATE INDEX IF NOT EXISTS idx_wb_feedback_workflow ON workbuddy_execution_feedback (tenant_id, workflow_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_wb_feedback_execution ON workbuddy_execution_feedback (tenant_id, execution_id);

-- The analyzer asks per kind -- how many corrections, how many supplied facts,
-- and what of each arrived recently -- so kind sits ahead of created_at while
-- the workflow prefix stays usable.
CREATE INDEX IF NOT EXISTS idx_wb_feedback_kind ON workbuddy_execution_feedback (tenant_id, workflow_id, kind, created_at DESC);

-- ── Row level security: one tenant per row ──────────────────────────────────

ALTER TABLE workbuddy_execution_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_execution_feedback FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_execution_feedback_tenant ON workbuddy_execution_feedback;
CREATE POLICY workbuddy_execution_feedback_tenant ON workbuddy_execution_feedback FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_execution_feedback FROM PUBLIC;

UPDATE _schema_version SET version = 37;

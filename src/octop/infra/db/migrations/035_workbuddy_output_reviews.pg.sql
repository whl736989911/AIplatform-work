-- Schema v35: an execution's outputs can be reviewed after the fact, and a
-- review adds a row plus a corrected value instead of rewriting the run
-- (PostgreSQL only).
--
-- The runtime the earlier versions build is deliberately closed: once an
-- execution settles, its ``outputs`` snapshot, its step runs and its edge runs
-- are execution facts, and the reconciliation of 019 reads them as evidence.
-- That is what makes a run trustworthy, and it is also what made the one
-- remaining gap in the loop unfixable: a person who reads a finished run and
-- sees that one field came back wrong -- a total that landed in the wrong
-- column, a customer name the model normalised away -- had no way to record
-- that judgement. The run had already settled, so there was nothing left to
-- block, and overwriting the outputs would have destroyed the very evidence the
-- reviewer was objecting to.
--
-- This version adds the third pause's opposite: no pause at all. A review is
-- written *after* the run settles, it never changes the execution's status, it
-- never touches the payload rows, and it never rewrites ``outputs``. The
-- reviewer's answer is an append-only product of its own:
--
--   workbuddy_output_reviews             one review per execution: the outputs
--                                        the run produced and the digest of
--                                        exactly those bytes, so a review can be
--                                        proven to be about the snapshot that was
--                                        actually shown; then the decision, the
--                                        values the reviewer put in their place,
--                                        and the execution a rerun was queued as.
--   workbuddy_output_review_reviewers    who should look at that output, and
--                                        what each of them did.
--
-- The trio of decisions is one vocabulary, not three: ``accepted`` says the
-- outputs stand as produced, ``corrected`` says a replacement value is attached,
-- ``rerun`` says the answer is a new execution whose id is recorded here. A
-- rerun therefore leaves two rows that both describe the same work: the original
-- run, unchanged, and the review that points at its successor. Nothing is
-- rewritten, so nothing is lost, and the audit trail of 018 reads the same as it
-- always did.
--
-- The pair is deliberately shaped like the approval pair of 018
-- (workbuddy_approval_requests / workbuddy_approval_candidates) and the question
-- pair of 034 (workbuddy_input_requests / workbuddy_input_assignees) so the
-- runtime keeps one review pattern instead of three.
--
-- Invariants enforced below:
--   * UUID identifiers and compound (tenant_id, execution_id) foreign keys, so a
--     row can never point at another tenant's execution.
--   * one review per execution: reviewing a settled run twice is the same
--     review, not a second opinion that would have to be reconciled with the
--     first.
--   * a corrected review carries a correction, and a correction is an object --
--     the same shape the run produced -- so a consumer can merge it into the
--     produced value without guessing what kind of value arrived.
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

-- ── The review of one settled execution ─────────────────────────────────────

-- ``produced`` and ``produced_sha256`` are written when the review opens, from
-- the settled execution, and are never updated afterwards: they are the thing
-- under review, so a review that could rewrite them would be reviewing itself.
-- ``corrected`` and ``corrected_sha256`` stay NULL until a reviewer replaces the
-- value, exactly as 034's answer columns do.
--
-- ``rerun_execution_id`` is a plain uuid and not a foreign key on purpose: the
-- review records what the rerun *was*, and a rerun that is later pruned must not
-- erase the decision that asked for it.
CREATE TABLE IF NOT EXISTS workbuddy_output_reviews (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  execution_id uuid NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'corrected', 'rerun')),
  produced jsonb NOT NULL,
  produced_sha256 text NOT NULL,
  corrected jsonb,
  corrected_sha256 text,
  requested_by_user_id integer,
  decided_by_user_id integer,
  decided_at timestamptz,
  rerun_execution_id uuid,
  locked_workflow_version_id uuid NOT NULL,
  locked_workflow_version_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, execution_id),
  FOREIGN KEY (tenant_id, execution_id) REFERENCES workbuddy_executions (tenant_id, id) ON DELETE CASCADE,
  CHECK (corrected IS NULL OR jsonb_typeof(corrected) = 'object'),
  CHECK (status <> 'corrected' OR corrected IS NOT NULL)
);

-- The review queue lists open reviews newest first and nothing else, so the
-- index covers the outstanding work instead of the whole history.
CREATE INDEX IF NOT EXISTS idx_wb_output_reviews_open ON workbuddy_output_reviews (tenant_id, created_at DESC)
  WHERE status = 'open';

-- ── Who should look at the output, and what each of them did ────────────────

-- A reviewer's status mirrors the review's decision vocabulary and adds the two
-- ways a reviewer can decline to decide: ``abstained`` (asked, deliberately did
-- not decide) and ``invalidated`` (asked, stopped being eligible before
-- deciding). Neither is a decision about the output, so neither is a review
-- status.
CREATE TABLE IF NOT EXISTS workbuddy_output_review_reviewers (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES workbuddy_tenants (tenant_id) ON DELETE CASCADE,
  review_id uuid NOT NULL,
  user_id integer NOT NULL,
  department_id uuid,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'corrected', 'rerun', 'abstained', 'invalidated')),
  decided_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, review_id, user_id),
  FOREIGN KEY (tenant_id, review_id) REFERENCES workbuddy_output_reviews (tenant_id, id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wb_output_review_reviewers_user ON workbuddy_output_review_reviewers (tenant_id, user_id, status, created_at DESC);

-- ── Row level security: one tenant per row ──────────────────────────────────

ALTER TABLE workbuddy_output_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_output_reviews FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_output_reviews_tenant ON workbuddy_output_reviews;
CREATE POLICY workbuddy_output_reviews_tenant ON workbuddy_output_reviews FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

ALTER TABLE workbuddy_output_review_reviewers ENABLE ROW LEVEL SECURITY;
ALTER TABLE workbuddy_output_review_reviewers FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS workbuddy_output_review_reviewers_tenant ON workbuddy_output_review_reviewers;
CREATE POLICY workbuddy_output_review_reviewers_tenant ON workbuddy_output_review_reviewers FOR ALL
  USING (workbuddy_rls_visible(tenant_id))
  WITH CHECK (workbuddy_rls_visible(tenant_id));

-- ── Privilege revocations (no implicit access for other roles) ───────────────

REVOKE ALL ON TABLE workbuddy_output_reviews FROM PUBLIC;
REVOKE ALL ON TABLE workbuddy_output_review_reviewers FROM PUBLIC;

UPDATE _schema_version SET version = 35;
